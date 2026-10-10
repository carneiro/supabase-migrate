from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import getpass
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import uuid

from .core import MigrationError, Project, digest, run, validate_refs, write_json
from .database import Database, MANAGED, application_inventory, connection_env, custom_role, role_statements
from .reporting import finish_report, observation, now
from .services import (capture_functions, capture_storage, clear_functions, clear_storage,
                       inspect_services, masked, restore_configs, restore_functions,
                       restore_storage, verify_services, verify_storage)

LIMITATIONS = [
    'Não existe atomicidade global entre PostgreSQL, Storage, Functions e APIs de configuração.',
    'Snapshot do banco; gravações posteriores na origem não são replicadas. Storage não tem snapshot global.',
    'Origem online e somente leitura; destino deve ficar sem clientes, jobs e gravações durante toda a operação.',
    'Secrets das Edge Functions não são copiados nem apagados; nomes são listados para ajuste manual.',
    'IDs/URLs, chaves API/JWT, domínio, DNS, plano/região/compute, backups/PITR e integrações externas não são clonados.',
    'Logs de plataforma, conexões Realtime em andamento e mensagens transitórias não são migrados.',
    'Callbacks OAuth e referências ao projeto antigo no código/SQL precisam de revisão; não se reescrevem dados arbitrários.',
    'Sessões Auth copiadas não tornam tokens JWT antigos válidos no destino: as chaves pertencem a cada projeto.',
]


def blockers(source, target, src_service, dst_service):
    errors = []
    if source['version'] // 10000 != target['version'] // 10000:
        errors.append('Versões major do PostgreSQL diferentes.')
    source_ext = {e['name']: e['version'] for e in source['extensions']}
    target_ext = {e['name']: e['version'] for e in target['extensions']}
    if source_ext != target_ext:
        errors.append('Extensões/versões diferentes; alinhe as extensões no destino antes da migração.')
    for side, inv, service in [('origem',source,src_service),('destino',target,dst_service)]:
        for key in ('storage.buckets_vectors','storage.vector_indexes','storage.buckets_analytics',
                    'storage.s3_multipart_uploads','vault.secrets','pgsodium.key'):
            if inv.get(key):
                errors.append(f'{side}: {key} contém estado que este adaptador não exporta com segurança.')
        if inv.get('foreign_servers') or inv.get('large_objects'):
            errors.append(f'{side}: FDW/large objects exigem adaptador específico para preservar dados externos/ownership.')
        if inv.get('custom_managed_tables'):
            errors.append(f'{side}: tabelas customizadas em auth/storage exigem migração explícita de schema.')
        if inv['subscriptions']:
            errors.append(f'{side}: subscriptions PostgreSQL exigem migração específica.')
        if inv.get('event_triggers'):
            errors.append(f'{side}: event triggers personalizados exigem privilégios/supervisão específicos.')
        if inv.get('managed_functions'):
            errors.append(f'{side}: funções personalizadas em auth/storage exigem adaptação explícita.')
        if any(r['rolcanlogin'] for r in inv['roles'] if custom_role(r['name'])):
            errors.append(f'{side}: roles LOGIN customizadas têm senhas não exportáveis; migração completa bloqueada.')
        if service.get('sso', {}).get('items') or service.get('third_party'):
            errors.append(f'{side}: SSO/Third-party Auth exige adaptação de identidades externas.')
        if masked(service['configs']):
            errors.append(f'{side}: API retornou configuração mascarada; não é possível copiar/restaurar esse segredo.')
        if inv.get('cron.job'):
            errors.append(f'{side}: Cron ativo/configurado pode gerar efeitos externos; migração automática de jobs ainda não suportada.')
        if any(b.get('type', 'STANDARD') not in (None,'STANDARD') for b in inv['buckets']):
            errors.append(f'{side}: bucket que não é Storage padrão.')
    def managed_tables(inv):
        return {(t['schema'],t['name']):t['columns'] for t in inv['tables'] if t['schema'] in ('auth','storage')}
    if source.get('auth_versions') != target.get('auth_versions') or source.get('storage_versions') != target.get('storage_versions'):
        errors.append('Versões internas Auth/Storage diferentes; aguarde/alinha atualizações da plataforma.')
    if managed_tables(source) != managed_tables(target):
        errors.append('Estruturas auth/storage incompatíveis; alinhe versões e customizações antes de continuar.')
    try:
        role_statements(source, target)
    except MigrationError as e:
        errors.append(str(e))
    common_custom = {r['name'] for r in source['roles'] if custom_role(r['name'])} & {r['name'] for r in target['roles']}
    membership = lambda inv: [m for m in inv['memberships'] if m['role'] in common_custom or m['member'] in common_custom]
    if membership(source) != membership(target):
        errors.append('Memberships de roles customizadas já existentes divergem; alinhe antes de continuar.')
    extra_roles = {r['name'] for r in target['roles'] if custom_role(r['name'])} - {r['name'] for r in source['roles']}
    if extra_roles:
        errors.append('Destino possui roles customizadas exclusivas; limpeza automática recusada.')
    return errors


def stable_inventory(inv):
    # GET object may update access telemetry; it is not evidence of content drift.
    result = {k:v for k,v in application_inventory(inv).items() if k not in ('checksums','row_counts','bytes')}
    result['objects'] = [{k:v for k,v in o.items() if k != 'last_accessed_at'} for o in inv['objects']]
    return result


def estimate(source, target, service, rate):
    object_bytes = sum(int((o.get('metadata') or {}).get('size', 0)) for i in (source,target) for o in i['objects'])
    objects = len(source['objects']) + len(target['objects'])
    # Download backup + restore + download verification; include DB checksum reads.
    seconds = 90 + ((source['bytes']+target['bytes'])*3 + object_bytes*3)/(rate*1024*1024)
    seconds += objects*0.6 + len(service['functions'])*12
    low = max(2, math.ceil(seconds/60))
    return {'min_minutes':low, 'max_minutes':low*3,
            'assumed_mib_s':rate, 'storage_bytes':object_bytes,
            'local_bytes_required':(source['bytes']+target['bytes'])*3 + object_bytes*2}


def save_manifest(folder):
    folder = Path(folder)
    hashes = {str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob('*'))
              if p.is_file() and p.name != 'manifest.json'}
    write_json(folder/'manifest.json', hashes)


def check_manifest(folder):
    folder = Path(folder).resolve()
    for relative, sha in json.loads((folder/'manifest.json').read_text()).items():
        path = (folder/relative).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or digest(path) != sha:
            raise MigrationError('Backup ausente, adulterado ou corrompido; recuperação recusada.')


def capture(project, db, inventory, services, folder, token):
    folder.mkdir()
    print('Capturando snapshot do banco de', project.ref, flush=True)
    db.dump(folder, inventory)
    write_json(folder/'services.json', services)


def capture_assets(project, inventory, services, folder, token):
    print('Capturando arquivos e funções de', project.ref, flush=True)
    capture_storage(project, inventory, folder)
    capture_functions(project, services, folder, token)
    save_manifest(folder)


def verify(project, db, inventory, services, folder):
    started = now()
    with db.consistent_snapshot():
        current = db.inventory()
        expected_scope, actual_scope = application_inventory(inventory), application_inventory(current)
        for key in ('schemas','tables','policies','triggers','publications','publication_tables',
                    'application_policies','application_triggers','sql_functions','views','indexes','constraints','sequences'):
            if actual_scope.get(key) != expected_scope.get(key):
                raise MigrationError('Verificação de schema falhou: ' + key)
        current['checksums'] = db.checksums(inventory)
        current['row_counts'] = dict(getattr(db,'last_row_counts',{}))
        if current['checksums'] != inventory['checksums']:
            raise MigrationError('Checksum das tabelas do banco diverge do snapshot.')
    verify_storage(project, db, inventory, folder)
    actual_services = verify_services(project, services)
    return observation(current,actual_services,started=started)


def replace(project, db, previous, incoming, previous_services, incoming_services, folder, token, checkpoint):
    # Every external write is preceded by a durable state transition.
    checkpoint('clearing_functions')
    clear_functions(project, previous_services)
    checkpoint('clearing_storage')
    clear_storage(project, previous)
    checkpoint('restoring_database')
    db.restore(folder, previous, incoming)
    checkpoint('restoring_config')
    restore_configs(project, incoming_services)
    checkpoint('restoring_storage')
    restore_storage(project, db, incoming, folder)
    checkpoint('restoring_functions')
    restore_functions(project, incoming_services, folder, token)
    checkpoint('verifying')
    return verify(project, db, incoming, incoming_services, folder)


def recover(project, db, folder, token, checkpoint):
    check_manifest(folder)
    incoming = json.loads((folder/'database.json').read_text())
    service = json.loads((folder/'services.json').read_text())
    current = db.inventory()
    current_service = inspect_services(project)
    replace(project, db, current, incoming, current_service, service, folder, token, checkpoint)


def parser():
    p = argparse.ArgumentParser(prog='./migrate', description='Migração Supabase com snapshot, validação e recuperação compensatória.')
    p.add_argument('--source', help='Project ref de origem (perguntado quando omitido).')
    p.add_argument('--target', help='Project ref de destino (perguntado quando omitido).')
    p.add_argument('--plan', action='store_true', help='Somente inventário/estimativa; sem dumps ou alterações remotas.')
    p.add_argument('--diff', action='store_true', help='Compara estados atuais (inclui checksums), sem migrar.')
    p.add_argument('--require-atomic', action='store_true', help='Recusa migração: atomicidade global não existe no Supabase.')
    p.add_argument('--work-dir', type=Path, default=Path('.migrate'), help='Diretório privado de backups e relatórios.')
    p.add_argument('--recover', type=Path, metavar='RUN_DIR', help='Recupera o destino usando backup de uma execução anterior.')
    p.add_argument('--throughput-mib', type=float, default=10, help='Taxa estimada em MiB/s (default: 10).')
    return p


def credentials(source, target):
    token = os.environ.get('SUPABASE_ACCESS_TOKEN') or getpass.getpass('Personal Access Token Supabase (entrada oculta): ')
    if not token:
        raise MigrationError('PAT necessário: https://supabase.com/dashboard/account/tokens. O login do MCP não é transferido ao script.')
    src_url = None
    if source:
        src_url = os.environ.get('SOURCE_DB_URL') or getpass.getpass('URL PostgreSQL de ORIGEM (Connect, entrada oculta): ')
    dst_url = os.environ.get('TARGET_DB_URL') or getpass.getpass('URL PostgreSQL de DESTINO (Connect, entrada oculta): ')
    return token, Database(connection_env(src_url,source),source=True) if source else None, Database(connection_env(dst_url,target))


def dependencies(*, readonly=False):
    for command in (('psql',) if readonly else ('psql','pg_dump','pg_restore','supabase')):
        if not shutil.which(command):
            raise MigrationError('Dependência ausente: ' + command + '. Consulte README.md.')
    if readonly:
        return
    for command in ('download','deploy'):
        help_text = run(['supabase','functions',command,'--help']).decode()
        if '--use-api' not in help_text or '--project-ref' not in help_text:
            raise MigrationError('Atualize Supabase CLI: functions download/deploy --use-api é necessário.')


def execute(args, context):
    if args.require_atomic:
        raise MigrationError('Atomicidade global indisponível: nenhuma alteração realizada. Use um cutover externo para esse requisito.')
    if args.throughput_mib <= 0 or not math.isfinite(args.throughput_mib):
        raise MigrationError('--throughput-mib deve ser positivo e finito.')
    if not sys.stdin.isatty() and not (args.plan or args.diff):
        raise MigrationError('Execução destrutiva exige terminal interativo e confirmação do ID do destino.')
    dependencies(readonly=args.plan or args.diff)
    os.umask(0o077)
    if args.recover:
        recover_dir = args.recover.resolve()
        state = json.loads((recover_dir/'state.json').read_text())
        source, target = state['source'], state['target']
    else:
        source = args.source or input('Project ID de ORIGEM: ').strip()
        target = args.target or input('Project ID de DESTINO: ').strip()
    validate_refs(source,target)
    work = args.work_dir.resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    if work.is_symlink() or work.stat().st_mode & 0o077:
        raise MigrationError('Diretório de trabalho deve ter permissão 0700.')
    with (work/(target+'.lock')).open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError('Já existe uma migração local para esse destino.') from None
        token, src_db, dst_db = credentials(None if args.recover else source, target)
        dst = Project(target, token)
        dst.connect()
        # Detect insufficient DB restore privileges before destructive operations.
        if not (args.plan or args.diff):
            dst_db.sql('BEGIN; SET LOCAL session_replication_role = replica; ROLLBACK;')
        if args.recover:
            if input('Para recuperar o backup, digite RECUPERAR ' + target + ': ') != 'RECUPERAR ' + target:
                raise MigrationError('Recuperação cancelada.')
            # Keep the original migration report; recovery has its own audit trail.
            report_dir=recover_dir/('recovery-'+uuid.uuid4().hex[:8])
            report_dir.mkdir(mode=0o700)
            state=dict(state,phase='recovering')
            context.update(run_dir=report_dir,state=state,target_db=dst_db,target_project=dst,
                           report_data=True,observations={},recovery_only=True)
            def recovery_checkpoint(phase):
                state['phase']=phase
                write_json(report_dir/'state.json',state)
                write_json(recover_dir/'state.json',state)
                print('Etapa:',phase,flush=True)
            try:
                recover(dst,dst_db,recover_dir/'target',token,recovery_checkpoint)
            except BaseException:
                recovery_checkpoint('recovery_failed')
                raise
            recovery_checkpoint('recovered')
            print('Destino recuperado e verificado. Secrets nunca foram alterados.')
            return 0
        src = Project(source, token, source=True)
        src.connect()
        run_dir = work/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
        run_dir.mkdir()
        state = {'source':source,'target':target,'phase':'inventory','global_atomic':False}
        context.update(run_dir=run_dir,state=state,source_db=src_db,target_db=dst_db,
                       source_project=src,target_project=dst,report_data=not args.plan,observations={})
        def checkpoint(phase):
            state['phase']=phase
            write_json(run_dir/'state.json',state)
            print('Etapa:',phase,flush=True)
        checkpoint('inventory')
        with ExitStack() as stack:
            stack.enter_context(src_db.consistent_snapshot())
            stack.enter_context(dst_db.consistent_snapshot())
            observed_at=now()
            source_inv,target_inv = src_db.inventory(),dst_db.inventory()
            src_service,dst_service = inspect_services(src),inspect_services(dst)
            context['observations']['source_snapshot']=observation(source_inv,src_service,started=observed_at)
            context['observations']['target_before']=observation(target_inv,dst_service,started=observed_at)
            problems = blockers(source_inv,target_inv,src_service,dst_service)
            timing = estimate(source_inv,target_inv,src_service,args.throughput_mib)
            report = {'source':source,'target':target,'estimate':timing,'limitations':LIMITATIONS,
                      'blockers':problems,'edge_secrets_manual':src_service['secret_names'],
                      'destination_secrets_retained':dst_service['secret_names'],
                      'inventory':{'source_tables':len(source_inv['tables']),
                                   'source_objects':len(source_inv['objects']),
                                   'source_functions':len(src_service['functions']),
                                   'target_objects':len(target_inv['objects']),
                                   'target_functions':len(dst_service['functions'])}}
            write_json(run_dir/'report.json',report)
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
            print(f"Estimativa: {timing['min_minutes']}–{timing['max_minutes']} minutos; depende de rede, quotas e volume.")
            print('Escopo: schemas/dados, Auth, RLS, funções SQL, publicações/config Realtime, Edge Functions, Storage e owners.')
            if args.diff:
                checkpoint('compared_only')
                return 0
            if problems:
                checkpoint('blocked')
                raise MigrationError('Preflight bloqueou a migração. Consulte os blockers do relatório; destino intacto.')
            if args.plan:
                checkpoint('planned')
                print('Plano salvo em',run_dir/'report.json')
                return 0
            if shutil.disk_usage(work).free < timing['local_bytes_required']:
                raise MigrationError('Espaço local insuficiente para backups e verificação.')
            print('ATENÇÃO: migração DESTRUTIVA. O conteúdo migrável do destino será apagado.')
            print('Não há transação global. Recuperação compensatória também pode falhar. Mantenha o destino sem clientes/gravações.')
            phrase = 'MIGRAR ' + target
            if input('Li os limites acima e isolei o destino. Para continuar digite '+phrase+': ') != phrase:
                raise MigrationError('Migração cancelada; destino intacto.')
            checkpoint('backup_target')
            capture(dst,dst_db,target_inv,dst_service,run_dir/'target',token)
            checkpoint('capture_source')
            capture(src,src_db,source_inv,src_service,run_dir/'source',token)
            problems = blockers(source_inv,target_inv,src_service,dst_service)
            if problems:
                raise MigrationError('; '.join(problems))
        capture_assets(dst,target_inv,dst_service,run_dir/'target',token)
        capture_assets(src,source_inv,src_service,run_dir/'source',token)
        # Check that Storage membership/versions and target content did not change
        # while binaries were captured; the origin remains online throughout.
        checkpoint('recheck')
        live_source,live_target = src_db.inventory(),dst_db.inventory()
        if stable_inventory(live_source)['objects'] != stable_inventory(source_inv)['objects'] or live_source['buckets'] != source_inv['buckets']:
            raise MigrationError('Storage da origem mudou durante o snapshot; refaça a captura. Destino intacto.')
        if stable_inventory(live_target) != stable_inventory(target_inv):
            raise MigrationError('Destino mudou durante o backup; isole o destino e tente novamente.')
        if dst_db.checksums(target_inv) != target_inv['checksums']:
            raise MigrationError('Dados do destino mudaram durante o backup. Destino intacto.')
        if inspect_services(src) != src_service or inspect_services(dst) != dst_service:
            raise MigrationError('Configuração/funções mudou durante backup. Destino intacto.')
        check_manifest(run_dir/'source')
        check_manifest(run_dir/'target')
        try:
            verified = replace(dst,dst_db,target_inv,source_inv,dst_service,src_service,run_dir/'source',token,checkpoint)
            if isinstance(verified,dict):
                context['observations']['target_verified']=verified
        except (Exception, KeyboardInterrupt):
            checkpoint('recovering')
            # Do not interrupt recovery again with Ctrl-C. SIGKILL still needs --recover.
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                recover(dst,dst_db,run_dir/'target',token,checkpoint)
                checkpoint('failed_recovered')
                print('Migração falhou. Backup do destino recuperado e verificado.',file=sys.stderr)
            except Exception:
                checkpoint('recovery_failed')
                print('Recuperação incompleta. Preserve o backup; execute ./migrate --recover '+str(run_dir),file=sys.stderr)
            finally:
                signal.signal(signal.SIGINT,old)
            raise MigrationError('Migração não concluída; consulte state.json e preserve os backups.') from None
        checkpoint('completed_pending_manual_secrets' if src_service['secret_names'] else 'completed')
        print('Cópia verificada. Secrets Edge para migração manual:')
        for name in src_service['secret_names']:
            print(' -',name)
        print('Backups privados e relatório:',run_dir)
        print('Revise callbacks/URLs e secrets antes de direcionar clientes ao destino.')
        return 0


def main(argv=None):
    args=parser().parse_args(argv)
    if sum(bool(x) for x in (args.plan,args.diff,args.recover)) > 1:
        raise MigrationError('Escolha apenas um modo: --plan, --diff ou --recover.')
    context={}
    result=1
    try:
        result=execute(args,context)
    except BaseException:
        if context:
            state=context['state']
            if state['phase'] not in ('failed_recovered','recovery_failed','blocked'):
                state['failed_at']=state['phase']
                state['phase']='failed_before_migration'
                write_json(Path(context['run_dir'])/'state.json',state)
        raise
    finally:
        if context:
            try:
                report=finish_report(context)
                if report['errors'] and result==0:
                    result=2
            except (Exception,KeyboardInterrupt):
                print('Não foi possível concluir o relatório; backups e estado da migração foram preservados.',file=sys.stderr)
                if result==0:
                    result=2
    return result
