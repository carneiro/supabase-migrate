"""Private, value-free feature reports. Comparison is never evidence of execution."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .core import write_json
from .database import MANAGED, application_inventory
from .services import inspect_services

FEATURES = ('database', 'auth', 'rls_functions', 'realtime', 'storage',
            'edge_functions', 'configuration', 'edge_secrets', 'extensions',
            'cron', 'vault', 'storage_vectors_analytics', 'platform')
LABELS = dict(zip(FEATURES, ('Database', 'Auth', 'RLS e funções SQL', 'Realtime',
    'Storage', 'Edge Functions', 'Configurações', 'Secrets Edge', 'Extensões',
    'Cron', 'Vault/pgsodium', 'Storage vetorial/analítico', 'Plataforma')))


def now():
    return datetime.now(timezone.utc).isoformat()


def observation(inventory, services, *, started=None, ended=None):
    return {'started_at': started or now(), 'finished_at': ended or now(),
            'inventory': inventory, 'services': services}


def observe(db, project, *, data=True):
    started = now()
    with db.consistent_snapshot():
        inventory = db.inventory()
        if data:
            inventory['checksums'] = db.checksums(inventory)
            inventory['row_counts'] = dict(getattr(db, 'last_row_counts', {}))
    service = inspect_services(project)
    return observation(inventory, service, started=started, ended=now())


def _name(*parts):
    # JSON tuples avoid collisions between identifiers containing dots/slashes.
    return json.dumps(parts, ensure_ascii=False, separators=(',', ':'))


def _flatten(value, prefix=''):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result.update(_flatten(item, prefix + '/' + key))
        return result
    return {prefix: value}


def feature_maps(obs):
    """Private comparison values stay in memory; only keys/statuses are emitted."""
    if obs is None:
        return None
    inv, service = obs['inventory'], obs['services']
    app = application_inventory(inv)
    maps = {key: {} for key in FEATURES}
    for schema in app.get('schemas', []):
        maps['database']['schema:' + schema['name']] = schema
    for table in inv.get('tables', []):
        key = _name(table['schema'], table['name'])
        feature = 'auth' if table['schema'] == 'auth' else 'database'
        if table['schema'] not in MANAGED or table['schema'] == 'auth':
            maps[feature]['table:' + key] = table
    for key, checksum in inv.get('checksums', {}).items():
        feature = 'auth' if key.startswith('auth.') else 'database'
        maps[feature]['data:' + key] = checksum
    for key in ('application_policies', 'application_triggers', 'sql_functions', 'views', 'indexes', 'constraints', 'sequences'):
        for item in inv.get(key, []):
            identity = tuple(item.get(f, '') for f in ('schema','table','name','identity'))
            maps['rls_functions'][key + ':' + _name(*identity)] = item
    for key in ('policies', 'triggers'):
        for item in inv.get(key, []):
            identity = tuple(item.get(f, '') for f in ('schema','table','name','schemaname','tablename','policyname'))
            maps['rls_functions'][key + ':' + _name(*identity)] = item
    for item in inv.get('managed_default_acl',[]):
        maps['rls_functions']['platform_default_acl:'+_name(item['role'],item['schema'],item['kind'])]=item
    for item in app.get('publications', []):
        maps['realtime']['publication:' + item['name']] = item
    for item in app.get('publication_tables', []):
        maps['realtime']['table:' + _name(item['pubname'],item['schemaname'],item['tablename'])] = item
    for bucket in inv.get('buckets', []):
        maps['storage']['bucket:' + bucket['id']] = {k:bucket.get(k) for k in
            ('name','public','owner','owner_id','file_size_limit','allowed_mime_types','type')}
    for obj in inv.get('objects', []):
        # File paths may contain patient names. Do not expose paths/owners/metadata.
        key = hashlib.sha256(_name(obj['bucket_id'],obj['name']).encode()).hexdigest()
        maps['storage']['object:' + key] = {k:obj.get(k) for k in ('id','owner','owner_id','user_metadata')}
        meta = obj.get('metadata') or {}
        maps['storage']['object:' + key].update({k:meta.get(k) for k in ('size','mimetype','cacheControl')})
    for f in service.get('functions', []):
        maps['edge_functions'][f['slug']] = {k:f.get(k) for k in ('name','verify_jwt','status')}
    for endpoint, config in service.get('configs', {}).items():
        maps['configuration'].update(_flatten(config,endpoint))
    maps['edge_secrets'] = {key: True for key in service.get('secret_names', [])}
    maps['extensions'] = {e['name']: e for e in inv.get('extensions', [])}
    maps['cron'] = {'jobs': inv.get('cron.job', 0)}
    maps['vault'] = {key: inv.get(key,0) for key in ('vault.secrets','pgsodium.key')}
    maps['storage_vectors_analytics'] = {key: inv.get(key,0) for key in
        ('storage.buckets_vectors','storage.vector_indexes','storage.buckets_analytics')}
    maps['platform'] = {'postgres_version': inv.get('version'),
        'auth_schema_versions': inv.get('auth_versions'), 'storage_schema_versions':inv.get('storage_versions'),
        'internal_realtime_tables': [t['name'] for t in inv.get('tables',[]) if t['schema']=='realtime'],
        'internal_realtime_publications': [p for p in inv.get('publications',[]) if p['name'].startswith('supabase_realtime_')]}
    return maps


def compare_maps(left, right, *, limit=200):
    if left is None or right is None:
        return {'status':'not_collected'}
    only_left = sorted(left.keys() - right.keys())
    only_right = sorted(right.keys() - left.keys())
    changed = sorted(k for k in left.keys() & right.keys() if left[k] != right[k])
    return {'status':'different' if only_left or only_right or changed else 'equal',
            'left_count':len(left),'right_count':len(right),
            'only_left_count':len(only_left),'only_right_count':len(only_right),'changed_count':len(changed),
            'only_left':only_left[:limit],'only_right':only_right[:limit],'changed':changed[:limit],
            'detail_limit_per_kind':limit,
            'truncated':any(len(v)>limit for v in (only_left,only_right,changed))}


def compare(left, right):
    lm, rm = feature_maps(left), feature_maps(right)
    result={feature:compare_maps(lm[feature] if lm else None,rm[feature] if rm else None) for feature in FEATURES}
    for feature in ('database','auth'):
        if left and right and ('checksums' not in left['inventory'] or 'checksums' not in right['inventory']):
            result[feature]=compare_maps({k:v for k,v in lm[feature].items() if not k.startswith('data:')},
                                        {k:v for k,v in rm[feature].items() if not k.startswith('data:')})
            result[feature]['status']='metadata_'+result[feature]['status']+'_data_not_checked'
    return result


def counts(obs):
    if obs is None:
        return None
    inv, service = obs['inventory'],obs['services']
    app = application_inventory(inv)
    rows = inv.get('row_counts')
    return {'application_tables':len([t for t in inv.get('tables',[]) if t['schema'] not in MANAGED]),
            'auth_users':(rows or {}).get('auth.users'),
            'tables_with_data_checksum':len(inv.get('checksums',{})),
            'row_counts':rows, 'buckets':len(inv.get('buckets',[])),
            'objects':len(inv.get('objects',[])),
            'storage_bytes':sum(int((o.get('metadata') or {}).get('size') or 0) for o in inv.get('objects',[])),
            'objects_with_owner':sum(o.get('owner') is not None for o in inv.get('objects',[])),
            'objects_with_owner_id':sum(o.get('owner_id') is not None for o in inv.get('objects',[])),
            'edge_functions':len(service.get('functions',[])),
            'application_publications':len(app.get('publications',[])),
            'policies':len(inv.get('application_policies',[]))+len(inv.get('policies',[])),
            'sql_functions':len(inv.get('sql_functions',[]))}


def build_report(context):
    observations = context.get('observations', {})
    snapshot = observations.get('source_snapshot')
    current_source = observations.get('source_current')
    target = observations.get('target_current')
    verified = observations.get('target_verified')
    diffs = {
        'snapshot_vs_verified_target':compare(snapshot,verified),
        'current_source_vs_current_target':compare(current_source,target),
        'source_snapshot_vs_current_source':compare(snapshot,current_source),
        'destination_before_vs_current':compare(observations.get('target_before'),target),
    }
    phase = context['state']['phase']
    migrated = phase in ('completed','completed_pending_manual_secrets')
    features = {}
    for feature in FEATURES:
        status = 'migrated_and_verified' if migrated else 'not_completed'
        if feature in ('platform','extensions'):
            status='destination_preserved'
        elif feature=='edge_secrets':
            status='manual_values_not_compared'
        elif feature in ('cron','vault','storage_vectors_analytics'):
            status='unsupported_if_present'
        features[feature]={'label':LABELS[feature],'migration_status':status,
            'snapshot_diff':diffs['snapshot_vs_verified_target'][feature]['status'],
            'current_diff':diffs['current_source_vs_current_target'][feature]['status']}
        if feature=='edge_functions':
            features[feature]['verification_scope']='deploy_and_metadata; runtime_not_invoked'
        if feature=='storage':
            features[feature]['verification_scope']='snapshot_bytes_sha256_and_owners; current_diff_metadata_only'
    function_hashes = lambda obs: {f['slug']:f.get('ezbr_sha256') for f in obs['services'].get('functions',[]) if f.get('ezbr_sha256')} if obs else None
    report = {'report_version':1,'generated_at':now(),'source':context['state']['source'],
        'target':context['state']['target'],'migration_phase':phase,'migration_completed':migrated,
        'comparison_complete':bool(current_source and target), 'features':features,'diffs':diffs,
        'recovery_only':context.get('recovery_only',False),
        'observations':{key:{'started_at':o['started_at'],'finished_at':o['finished_at'],
            'data_checksums_collected':'checksums' in o['inventory'],'counts':counts(o)} for key,o in observations.items()},
        'edge_bundle_hash_comparison':compare_maps(function_hashes(current_source),function_hashes(target)),
        'edge_secrets_manual':list((snapshot or current_source or {}).get('services',{}).get('secret_names',[])),
        'errors':context.get('report_errors',[]),
        'caveats':[
            'Os dois estados atuais são observados em janelas distintas; a origem continua online.',
            'Diferenças atuais podem ser gravações posteriores ao snapshot; não comprovam falha da cópia.',
            'Valores de configuração, dados pessoais, SQL e owners não são expostos; só nomes/chaves e contagens de diferenças.',
            'Storage atual compara inventário e metadados. Bytes são verificados durante a migração, não baixados novamente para o diff.',
            'SHA de bundle Edge diferente não prova diferença semântica: o empacotamento pode conter IDs do projeto. Runtime não invocado.',
            'Secrets Edge: somente nomes; igualdade dos valores nunca é presumida.',
            'Schemas/publicações internos do Realtime pertencem à plataforma e são preservados no destino.'
        ]}
    if context.get('recovery_only'):
        report['caveats'].append('Recuperação manual observa apenas o destino; use --diff depois para comparar com a origem.')
    return report


def write_report(context):
    report = build_report(context)
    folder = Path(context['run_dir'])
    write_json(folder/'final-report.json',report)
    lines=['# Relatório de migração Supabase','',
           'Origem: '+report['source']+' — Destino: '+report['target'],
           '','Estado: `'+report['migration_phase']+'`',
           '','| Recurso | Migração | Snapshot × destino verificado | Origem atual × destino atual |',
           '| --- | --- | --- | --- |']
    for item in report['features'].values():
        lines.append('| '+' | '.join(item[k] for k in ('label','migration_status','snapshot_diff','current_diff'))+' |')
    lines += ['', '| Observação | Tabelas aplicação | Usuários Auth | Buckets | Arquivos | Bytes Storage | Edge Functions |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    for name,item in report['observations'].items():
        c=item['counts']
        lines.append('| '+name+' | '+' | '.join(str(c[k]) if c[k] is not None else 'não contado' for k in
            ('application_tables','auth_users','buckets','objects','storage_bytes','edge_functions'))+' |')
    lines += ['', 'Secrets Edge para migração manual: '+(', '.join(report['edge_secrets_manual']) or 'nenhum nome coletado')+'.']
    lines += ['','Os detalhes completos, contagens e janelas de observação estão em `final-report.json`.','']
    lines += ['- '+note for note in report['caveats']]
    if report['errors']:
        lines += ['','Comparação incompleta: '+', '.join(report['errors'])]
    (folder/'final-report.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    (folder/'final-report.md').chmod(0o600)
    return report


def finish_report(context):
    """Report failures never trigger a destructive rollback of a successful copy."""
    context.setdefault('observations',{})
    for side in ('source','target'):
        db,project=context.get(side+'_db'),context.get(side+'_project')
        if db is None or project is None:
            continue
        try:
            context['observations'][side+'_current']=observe(db,project,data=context.get('report_data',True))
        except Exception:
            context['observations'].pop(side+'_current',None)
            context.setdefault('report_errors',[]).append(side+'_observation_failed')
    report=write_report(context)
    print('Relatório final:',Path(context['run_dir'])/'final-report.md',flush=True)
    if report['errors']:
        print('Diff incompleto; consulte final-report.json. Nenhum sucesso de comparação presumido.',flush=True)
    return report
