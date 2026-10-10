from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.parse import quote

from .core import MigrationError, digest, identifier as qi, literal as ql, run, write_json

CONTRACTS = json.loads(Path(__file__).with_name('config_contracts.json').read_text())
BUILTIN_SECRETS = {'SUPABASE_URL','SUPABASE_ANON_KEY','SUPABASE_SERVICE_ROLE_KEY','SUPABASE_DB_URL'}


def writable_config(value, schema):
    if isinstance(value, dict) and 'properties' in schema:
        return {k: writable_config(v, schema['properties'][k]) for k, v in value.items()
                if k in schema['properties']}
    return value


def masked(value):
    if isinstance(value, dict):
        return any(masked(v) for v in value.values())
    return isinstance(value, str) and bool(re.fullmatch(r'\*{3,}|\[REDACTED\]|<redacted>', value, re.I))


def inspect_services(project):
    result = {'functions': project.api.request('GET', '/functions'),
              'secret_names': sorted(x['name'] for x in project.api.request('GET', '/secrets')
                                     if x['name'] not in BUILTIN_SECRETS),
              'configs': {}}
    for endpoint, schema in CONTRACTS.items():
        raw = project.api.request('GET', '/' + endpoint)
        result['configs'][endpoint] = writable_config(raw, schema)
    # These identities are not portable just by copying SQL auth tables.
    result['sso'] = project.api.request('GET', '/config/auth/sso/providers')
    result['third_party'] = project.api.request('GET', '/config/auth/third-party-auth')
    return result


def object_path(obj):
    return '/object/' + quote(obj['bucket_id'], safe='') + '/' + quote(obj['name'], safe='/')


def capture_storage(project, inventory, folder):
    folder = Path(folder) / 'objects'
    folder.mkdir()
    manifest = []
    for index, obj in enumerate(inventory['objects']):
        local = folder / f'{index:012d}.bin'
        sha = project.storage.request('GET', object_path(obj).replace('/object/', '/object/authenticated/', 1), download=local)
        expected = (obj.get('metadata') or {}).get('size')
        if expected is not None and local.stat().st_size != int(expected):
            raise MigrationError('Objeto Storage mudou durante captura; destino permanece intacto.')
        manifest.append({'file': local.name, 'sha256': sha, 'object': obj})
        if (index+1)%100==0 or index+1==len(inventory['objects']):
            print(f"Storage capturado: {index+1}/{len(inventory['objects'])}",flush=True)
    write_json(folder.parent / 'objects.json', manifest)
    return manifest


def clear_storage(project, inventory):
    for bucket in inventory['buckets']:
        names = [o['name'] for o in inventory['objects'] if o['bucket_id'] == bucket['id']]
        for start in range(0, len(names), 100):
            project.storage.request('DELETE', '/object/' + quote(bucket['id'], safe=''),
                                    {'prefixes': names[start:start + 100]})
        project.storage.request('DELETE', '/bucket/' + quote(bucket['id'], safe=''))


def restore_storage(project, db, inventory, folder):
    folder = Path(folder)
    for b in inventory['buckets']:
        body = {k: b[k] for k in ('id','name','public','file_size_limit','allowed_mime_types') if k in b}
        project.storage.request('POST', '/bucket', body)
    manifest = json.loads((folder/'objects.json').read_text())
    for index,item in enumerate(manifest,1):
        obj = item['object']
        path = folder/'objects'/item['file']
        if digest(path) != item['sha256']:
            raise MigrationError('Checksum do backup Storage inválido.')
        meta = obj.get('metadata') or {}
        headers = {'Content-Type': meta.get('mimetype') or 'application/octet-stream',
                   'Cache-Control': meta.get('cacheControl') or 'max-age=3600', 'x-upsert': 'false'}
        project.storage.request('POST', object_path(obj), upload=path, headers=headers)
        # Keep destination's physical version/ETag, but preserve identity and ownership.
        fields = [k for k in ('id','owner','owner_id','created_at','updated_at','last_accessed_at','user_metadata') if k in obj]
        assignments = []
        for key in fields:
            value = obj[key]
            encoded = 'NULL' if value is None else ql(json.dumps(value) if isinstance(value, (dict, list)) else str(value))
            assignments.append(qi(key) + '=' + encoded)
        # Platform update triggers otherwise replace the captured updated_at.
        db.sql('BEGIN; SET LOCAL session_replication_role=replica; UPDATE storage.objects SET ' + ','.join(assignments) + ' WHERE bucket_id=' + ql(obj['bucket_id']) + ' AND name=' + ql(obj['name']) + '; COMMIT;')
        if index%100==0 or index==len(manifest):
            print(f'Storage restaurado: {index}/{len(manifest)}',flush=True)
    for b in inventory['buckets']:
        fields = [k for k in ('owner','owner_id','created_at','updated_at') if k in b]
        if fields:
            assignments = [qi(k) + '=' + ('NULL' if b[k] is None else ql(str(b[k]))) for k in fields]
            db.sql('BEGIN; SET LOCAL session_replication_role=replica; UPDATE storage.buckets SET ' + ','.join(assignments) + ' WHERE id=' + ql(b['id']) + '; COMMIT;')


def verify_storage(project, db, inventory, folder):
    buckets={b['id']:b for b in db.rows('SELECT * FROM storage.buckets ORDER BY id')}
    expected_buckets={b['id']:b for b in inventory['buckets']}
    if buckets.keys()!=expected_buckets.keys():
        raise MigrationError('Verificação Storage: conjunto de buckets diferente.')
    for key,bucket in expected_buckets.items():
        for field in ('name','public','owner','owner_id','file_size_limit','allowed_mime_types','created_at','updated_at'):
            if bucket.get(field)!=buckets[key].get(field):
                raise MigrationError('Verificação Storage: configuração/owner de bucket diferente.')
    current = db.rows('SELECT * FROM storage.objects ORDER BY bucket_id,name')
    manifest = json.loads((Path(folder)/'objects.json').read_text())
    expected = {(o['bucket_id'],o['name']): o for o in inventory['objects']}
    actual = {(o['bucket_id'],o['name']): o for o in current}
    if expected.keys() != actual.keys():
        raise MigrationError('Verificação Storage: conjunto de objetos diferente.')
    for key, obj in expected.items():
        for field in ('id','owner','owner_id','user_metadata','created_at','updated_at'):
            if obj.get(field) != actual[key].get(field):
                raise MigrationError('Verificação Storage: identidade/owner/metadados diferentes.')
        metadata=obj.get('metadata') or {}
        for field in ('size','mimetype','cacheControl'):
            if field in metadata and metadata[field]!=(actual[key].get('metadata') or {}).get(field):
                raise MigrationError('Verificação Storage: tamanho/MIME/cache diferentes.')
    check = Path(folder)/'verify-object.tmp'
    try:
        for index,item in enumerate(manifest,1):
            sha = project.storage.request('GET', object_path(item['object']).replace('/object/', '/object/authenticated/', 1), download=check)
            if sha != item['sha256']:
                raise MigrationError('Verificação Storage: conteúdo do arquivo divergente.')
            if index%100==0 or index==len(manifest):
                print(f'Storage verificado: {index}/{len(manifest)}',flush=True)
    finally:
        check.unlink(missing_ok=True)


def cli_env(token):
    return dict(os.environ, SUPABASE_ACCESS_TOKEN=token)


def capture_functions(project, service, folder, token):
    folder = Path(folder)
    (folder/'supabase').mkdir(exist_ok=True)
    config = ['project_id = ' + json.dumps(project.ref)]
    (folder/'supabase'/'config.toml').write_text('\n'.join(config)+'\n')
    for f in service['functions']:
        slug = f['slug']
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', slug):
            raise MigrationError('Slug Edge Function inválido.')
        run(['supabase','functions','download',slug,'--project-ref',project.ref,'--use-api',
             '--workdir',str(folder)], env=cli_env(token))
        root = folder/'supabase'/'functions'/slug
        # The API does not guarantee restoration of import maps/deno.json.
        if f.get('import_map') or f.get('import_map_path'):
            raise MigrationError('Edge Function usa import map; forneça bundle autocontido antes da migração: ' + slug)
        entry = root/'index.ts'
        if not entry.is_file():
            raise MigrationError('Entrypoint index.ts ausente no download da função: ' + slug)
        if any(p.is_symlink() for p in root.rglob('*')):
            raise MigrationError('Link simbólico no bundle de função recusado.')
        config += ['', '[functions.' + json.dumps(slug) + ']',
                   'verify_jwt = ' + str(f.get('verify_jwt', True)).lower(),
                   'entrypoint = ' + json.dumps('./functions/' + slug + '/index.ts')]
    (folder/'supabase'/'config.toml').write_text('\n'.join(config)+'\n')


def clear_functions(project, service):
    for f in service['functions']:
        project.api.request('DELETE', '/functions/' + quote(f['slug'], safe=''))


def restore_functions(project, service, folder, token):
    for f in service['functions']:
        args = ['supabase','functions','deploy',f['slug'],'--project-ref',project.ref,
                '--use-api','--workdir',str(folder)]
        if f.get('verify_jwt') is False:
            args.append('--no-verify-jwt')
        run(args, env=cli_env(token))
        if f.get('name') and f['name'] != f['slug']:
            project.api.request('PATCH', '/functions/' + quote(f['slug'], safe=''), {'name':f['name']})


def restore_configs(project, service):
    for endpoint, body in service['configs'].items():
        project.api.request('PATCH', '/' + endpoint, body)


def verify_services(project, expected):
    actual = inspect_services(project)
    fields = lambda functions: sorted((f['slug'], f.get('name',f['slug']), f.get('verify_jwt', True)) for f in functions)
    if fields(actual['functions']) != fields(expected['functions']):
        raise MigrationError('Verificação de Edge Functions falhou.')
    for endpoint, config in expected['configs'].items():
        if config != actual['configs'][endpoint]:
            raise MigrationError('Verificação de configurações falhou: ' + endpoint)
    return actual
