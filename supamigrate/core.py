from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request


class MigrationError(Exception):
    pass


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def validate_refs(source, target):
    for ref in (source, target):
        if not re.fullmatch(r'[a-z]{20}', ref):
            raise MigrationError('Project ID inválido: use o project ref de 20 letras minúsculas.')
    if source == target:
        raise MigrationError('Origem e destino precisam ser projetos diferentes.')


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    temp.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(args, *, env=None, cwd=None, output=None, input=None):
    # Never put credentials in argv or include subprocess stderr in public errors.
    result = subprocess.run(args, env=env, cwd=cwd, input=input,
                            stdout=output or subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise MigrationError(f'{Path(args[0]).name} falhou (código {result.returncode}); '
                             'detalhes omitidos para proteger credenciais e dados.')
    return result.stdout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MigrationError('Redirecionamento HTTP recusado para proteger credenciais.')


class HTTP:
    def __init__(self, base, token, *, source=False, apikey=False):
        self.base = base.rstrip('/')
        self.token = token
        self.source = source
        self.apikey = apikey
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None, *, download=None, upload=None, headers=None):
        if self.source and method != 'GET':
            raise MigrationError('Tentativa de escrita na origem bloqueada.')
        if (path and not path.startswith('/')) or path.startswith('//'):
            raise MigrationError('Caminho HTTP inválido.')
        request_headers = {'Authorization': 'Bearer ' + self.token}
        if self.apikey:
            request_headers['apikey'] = self.token
        request_headers.update(headers or {})
        if data is not None:
            data = json.dumps(data).encode()
            request_headers['Content-Type'] = 'application/json'
        # Retry reads only. Mutations can have succeeded despite a lost response.
        for attempt in range(4 if method == 'GET' else 1):
            f = None
            try:
                body = data
                if upload:
                    f = Path(upload).open('rb')
                    body = f
                    request_headers['Content-Length'] = str(Path(upload).stat().st_size)
                req = urllib.request.Request(self.base + path, data=body,
                                             headers=request_headers, method=method)
                with self.opener.open(req, timeout=120) as response:
                    if download:
                        h = hashlib.sha256()
                        with Path(download).open('wb') as dest:
                            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                                dest.write(chunk)
                                h.update(chunk)
                        return h.hexdigest()
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if method == 'GET' and e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise MigrationError(f'HTTP {e.code}: {method} {path.split("?")[0]}') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if method == 'GET' and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise MigrationError('Falha de conexão HTTP; nenhum sucesso presumido.') from None
            finally:
                if f:
                    f.close()


class Project:
    def __init__(self, ref, token, *, source=False):
        self.ref, self.source = ref, source
        self.api = HTTP('https://api.supabase.com/v1/projects/' + ref, token, source=source)
        self.storage = None

    def connect(self):
        info = self.api.request('GET', '')
        if info.get('id') != self.ref or info.get('status') != 'ACTIVE_HEALTHY':
            raise MigrationError('Projeto não está ACTIVE_HEALTHY ou ID não confere.')
        keys = self.api.request('GET', '/api-keys')
        service = next((k['api_key'] for k in keys if k.get('name') == 'service_role'), None)
        if not service:
            raise MigrationError('Chave service_role indisponível; migração Storage exige acesso administrativo.')
        self.storage = HTTP(f'https://{self.ref}.supabase.co/storage/v1', service,
                            source=self.source, apikey=True)
        return info
