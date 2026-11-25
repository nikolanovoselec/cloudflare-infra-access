#!/usr/bin/env python3
"""One isolated attempt per explicit authorization. No automatic mutation retry."""
import datetime as dt
import hashlib
from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from uuid import UUID

HERE = Path(__file__).resolve().parent
CLASS = 'AlphaProbeContainer'
MODULES = ('worker.mjs', 'probe.mjs', 'greeting.mjs')
SUBDOMAIN_API_DATE = ['-H', 'Cloudflare-Workers-Script-Api-Date: 2025-08-01']
STAGES = {'expired', 'already-used', 'durable-object', 'identity-fetcher', 'tcp-registration',
          'container-start', 'container-result', 'container-exit', 'greeting', 'non-ssh', 'runtime-config',
          'tcp-timeout', 'tcp-error', 'tcp-closed'}
SAFE_ERROR_TEXT = {
    10015: 'You do not have access to use Workers VPC Gateway Identity. Please ensure it is enabled. If you are an Enterprise user, reach out to your account team.',
    10007: 'This Worker does not exist on your account.',
    10000: 'Authentication error',
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def canonical_id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', value)


class Stop(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Stop(reason)


def runtime_config():
    message = 'CLOUDFLARE_RUNTIME_CONFIG must specify an approved identity and private SSH target with matching resource IDs'
    try:
        config = json.loads(os.environ.get('CLOUDFLARE_RUNTIME_CONFIG', ''))
        require(isinstance(config, dict) and set(config) == {
            'identity', 'ip', 'target_id', 'network_id', 'access_app_id', 'policy_id', 'target_name', 'ssh_username',
        } and all(isinstance(v, str) for v in config.values()), message)
        ip = IPv4Address(config['ip'])
        require(any(ip in IPv4Network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), message)
        require(all(canonical_id(config[key]) for key in ('target_id', 'network_id', 'access_app_id', 'policy_id')), message)
        require(re.fullmatch(r'[^\s@]+@[^\s@]+', config['identity']) and len(config['identity']) <= 254
                and re.fullmatch(r'[a-zA-Z0-9_.-]{1,253}', config['target_name'])
                and re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', config['ssh_username']), message)
    except ValueError:
        raise Stop(message) from None
    return config


class Attempt:
    def __init__(self, mode):
        nonce = secrets.token_hex(16)
        self.name = f'cf-ia-do-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}-{nonce}'
        self.owner = f'owner-{nonce}'
        self.path = f'/workers/scripts/{self.name}'
        self.apps = f'/containers/applications?name={self.name}'
        self.folder = HERE / self.name
        self.folder.mkdir(mode=0o700)
        self.api_token = os.environ.get('CLOUDFLARE_API_TOKEN')
        self.state = {'mode': mode, 'name': self.name, 'started_at': now(), 'upload_calls': 0}
        self.save()

    def save(self):
        dest = self.folder / 'summary.safe.json'
        tmp = dest.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, indent=2) + '\n')
        tmp.replace(dest)

    def call(self, stage, path, method='GET', *, data=None, multipart=None, host=None, bearer=None):
        token = self.api_token if host is None else bearer
        cfg = '' if not token else 'header = ' + json.dumps(f'Authorization: Bearer {token}') + '\n'
        args = ['curl', '-q', '--config', '-', '--proto', '=https', '--silent', '--retry', '0',
                '--connect-timeout', '8', '--max-time', '55' if host and bearer else '15',
                '--request', method, '--write-out', '\n%{http_code}\n%header{cf-ray}\n%header{date}']
        if path.endswith('/subdomain') and path.startswith(self.path + '/'):
            args += SUBDOMAIN_API_DATE
        if multipart is not None:
            args += ['--form', f'metadata=<{multipart};type=application/json']
            for name in MODULES:
                args += ['--form', f'{name}=@{HERE / name};type=application/javascript+module']
        if data is not None:
            args += ['-H', 'Content-Type: application/json', '--data-binary', json.dumps(data)]
        child_env = {key: val for key, val in os.environ.items()
                     if key not in {'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_RUNTIME_CONFIG'}}
        start = now()
        proc = subprocess.run(args + [(host or self.api) + path], input=cfg, text=True, errors='replace',
                              capture_output=True, env=child_env)
        raw, _, trailer = proc.stdout.rpartition('\n')
        raw, _, ray = raw.rpartition('\n')
        raw, _, status = raw.rpartition('\n')
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        body = parsed if isinstance(parsed, dict) else {}
        status = int(status) if re.fullmatch(r'[0-9]{3}', status) else 0
        event = {'stage': stage, 'method': method, 'started_at': start, 'ended_at': now(),
                 'status': status, 'curl_exit': proc.returncode, 'cf_ray': ray if re.fullmatch(r'[0-9a-fA-F]+-[A-Z]{3}', ray) else None,
                 'http_date': trailer if re.fullmatch(r'[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9:]{8} GMT', trailer) else None}
        errors = body.get('errors')
        codes = []
        if isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict) and type(code := err.get('code')) is int:
                    item = {'code': code}
                    if code in SAFE_ERROR_TEXT and err.get('message') == SAFE_ERROR_TEXT[code]:
                        item['message'] = SAFE_ERROR_TEXT[code]
                    codes.append(item)
        if isinstance(body.get('success'), bool):
            event['api_success'] = body['success']
        if codes:
            event['errors'] = codes
        if host:
            if raw == 'Not found':
                event['probe_response'] = 'Not found'
            elif isinstance(body.get('stage'), str) and body['stage'] in STAGES:
                event['probe_response'] = {k: v for k, v in body.items()
                                           if k == 'stage' or (k in {'registered', 'tcpConnected', 'sshGreeting', 'containerStopped'} and isinstance(v, bool))}
        with (self.folder / 'http.safe.jsonl').open('a') as log:
            log.write(json.dumps(event) + '\n')
        ok = proc.returncode == 0 and 200 <= status < 300 and body.get('success') is True
        return event, body, ok

    def readback_worker(self, stage):
        event, body, ok = self.call(stage, self.path + '/settings')
        absent = event['curl_exit'] == 0 and event['status'] == 404 and any(
            e['code'] == 10007 for e in event.get('errors', []))
        result = body.get('result', {}) if ok else {}
        owned = ok and ((result.get('annotations') or {}).get('workers/tag') == self.owner or self.owner in (result.get('tags') or []))
        return absent, owned, result

    def apps_absent(self, stage):
        _, body, ok = self.call(stage, self.apps)
        return ok and body.get('result') == [] and not (body.get('result_info') or {}).get('next_page_token')

    def app_owned(self, result):
        return (result.get('id') == self.namespace and result.get('name') == self.name
                and result.get('account_id') == self.account_id and result.get('scheduling_policy') == 'durable_object'
                and (result.get('durable_objects') or {}).get('namespace_id') == self.namespace)

    def preflight(self, config):
        def get(stage, path):
            _, body, ok = self.call(stage, path)
            require(ok, f'{stage} failed')
            return body['result'], body.get('result_info') or {}

        target, _ = get('fixed-target', f"/infrastructure/targets/{config['target_id']}")
        require(target['id'] == config['target_id'] and target['hostname'] == config['target_name'] and target['target_type'] == 'ip'
                and target['ip']['ipv4'] == {'ip_addr': config['ip'], 'virtual_network_id': config['network_id']}, 'Target no longer matches scope')
        app, _ = get('fixed-access-app', f"/access/apps/{config['access_app_id']}")
        require(app['id'] == config['access_app_id'] and app['type'] == 'infrastructure' and app['target_criteria'] == [
            {'include': {'target_attributes': {'hostname': [config['target_name']]}}, 'port': 22, 'protocol': 'SSH'}], 'Access app no longer matches scope')
        policies, info = get('fixed-access-policies', f"/access/apps/{config['access_app_id']}/policies")
        require(len(policies) == 1 and info.get('total_count', 1) == 1, 'Policy list changed')
        policy = policies[0]
        rules = policy['connection_rules']['ssh']
        require(policy['id'] == config['policy_id'] and policy['decision'] == 'allow' and policy['require'] == [{'email': {'email': config['identity']}}]
                and len(policy['include']) == 1 and bool(policy['include'][0].get('device_posture', {}).get('integration_uid'))
                and policy.get('exclude') == [] and rules['usernames'] == [config['ssh_username']] and rules['allow_email_alias'] is False,
                'Policy no longer matches identity, device-posture requirement or SSH scope')
        subdomain, _ = get('account-workers-subdomain', '/workers/subdomain')
        suffix = subdomain.get('subdomain', '')
        require(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', suffix), 'No valid existing workers.dev subdomain')
        self.state['scope_readback_matched'] = True
        return f'https://{self.name}.{suffix}.workers.dev'

    def run(self):
        require(self.api_token, 'Configured CLOUDFLARE_API_TOKEN is unavailable')
        account_id = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
        require(re.fullmatch(r'[0-9a-fA-F]{32}', account_id),
                'Set CLOUDFLARE_ACCOUNT_ID to the 32-character hexadecimal test account ID')
        self.account_id = account_id
        self.api = f'https://api.cloudflare.com/client/v4/accounts/{account_id}'
        runtime = self.state['mode'] == 'runtime'
        if runtime:
            self.state.update(app_create_calls=0, authenticated_calls=0, runtime_passed=False)
        config = runtime_config() if runtime else None
        host = self.preflight(config) if runtime else None
        absent, _, _ = self.readback_worker('worker-absence-before')
        require(absent, 'Generated Worker name absence not proven')
        self.state['worker_absent_before'] = True
        # Keep the admission payload the same in both modes except for probe authorization.
        metadata = {'main_module': MODULES[0], 'compatibility_date': '2026-07-09',
                    'compatibility_flags': ['nodejs_compat', 'experimental'],
                    'annotations': {'workers/tag': self.owner}, 'tags': [self.owner],
                    'bindings': [
                        {'name': 'GATEWAY_IDENTITY', 'type': 'vpc_network', 'network_id': 'cf1:network', 'identity': 'runtime-email-alpha'},
                        {'name': 'PROBE', 'type': 'durable_object_namespace', 'class_name': CLASS},
                    ], 'containers': [{'name': self.name, 'class_name': CLASS}],
                    'migrations': {'new_tag': 'v1', 'steps': [{'new_sqlite_classes': [CLASS]}]}}
        bearer = secrets.token_hex(32) if runtime else None
        expiry = time.time_ns() // 1_000_000 + 300_000 if runtime else None
        if runtime:
            metadata['bindings'].extend([
                {'name': 'EXPIRES_AT', 'type': 'plain_text', 'text': '[redacted]'},
                {'name': 'PROBE_TOKEN_HASH', 'type': 'secret_text', 'text': '[redacted]'},
                {'name': 'PROBE_IDENTITY', 'type': 'secret_text', 'text': '[redacted]'},
                {'name': 'PROBE_IP', 'type': 'secret_text', 'text': '[redacted]'},
            ])
        (self.folder / 'metadata.safe.json').write_text(json.dumps(metadata, indent=2) + '\n')
        self.state['module_sha256'] = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in MODULES}
        self.state['upload_calls'] = 1
        self.save()
        if runtime:
            with tempfile.TemporaryDirectory(prefix='infra-mvp-upload-') as private:
                meta = Path(private) / 'metadata.json'
                values = dict(EXPIRES_AT=str(expiry), PROBE_TOKEN_HASH=hashlib.sha256(bearer.encode()).hexdigest(),
                              PROBE_IDENTITY=config['identity'], PROBE_IP=config['ip'])
                for item in metadata['bindings']:
                    if item['name'] in values:
                        item['text'] = values[item['name']]
                meta.write_text(json.dumps(metadata))
                _, body, admitted = self.call('worker-upload', self.path, 'PUT', multipart=meta)
        else:
            _, body, admitted = self.call('worker-upload', self.path, 'PUT', multipart=self.folder / 'metadata.safe.json')
        self.state['upload_admitted'] = admitted
        require(admitted, 'Upload rejected or ambiguous')
        require(body['result'].get('id') == self.name, 'Upload response Worker identifier did not match')
        _, owned, settings = self.readback_worker('worker-ownership')
        require(owned, 'Worker ownership marker not visible')
        _, sub, ok = self.call('subdomain-after-upload', self.path + '/subdomain')
        self.state['endpoints_disabled'] = (ok and sub['result'].get('enabled') is False
                                           and sub['result'].get('previews_enabled') is False)
        require(self.state['endpoints_disabled'], 'Worker endpoint and preview not verified disabled after upload')
        if runtime:
            self.run_runtime(settings, body['result'], host, bearer, expiry)
        else:
            self.state['result'] = 'Alpha binding upload admitted; no runtime test performed'

    def run_runtime(self, settings, upload, host, bearer, expiry):
        bindings = settings.get('bindings') or []
        if not any(binding.get('namespace_id') for binding in bindings if binding.get('name') == 'PROBE'):
            version = upload.get('version_id') or upload.get('deployment_id', '')
            require(canonical_id(version), 'No version ID to resolve the exact DO namespace')
            if len(version) == 32:
                version = str(UUID(version))
            _, detail, ok = self.call('worker-version', self.path + '/versions/' + version)
            require(ok and detail['result'].get('id') == version, 'Worker version not confirmed')
            bindings = detail['result']['resources']['bindings']
        matching = [x.get('namespace_id') for x in bindings if x.get('type') == 'durable_object_namespace'
                    and x.get('name') == 'PROBE' and x.get('class_name') == CLASS and x.get('script_name') in (None, self.name)]
        require(len(matching) == 1 and canonical_id(matching[0]), 'Exact namespace ID not resolved')
        self.namespace = self.state['namespace_id'] = matching[0]
        self.app_path = '/containers/applications/' + self.namespace
        require(self.apps_absent('app-name-absence-before'), 'Container application name availability not proven')
        event, _, _ = self.call('app-id-absence-before', self.app_path)
        require(event['curl_exit'] == 0 and event['status'] == 404, 'Container application ID availability not proven')
        self.state['app_absent_before'] = True
        self.state['app_create_calls'] = 1
        self.save()
        _, app, ok = self.call('app-create', '/containers/applications', 'POST', data={
            'name': self.name, 'scheduling_policy': 'durable_object', 'durable_objects': {'namespace_id': self.namespace},
        })
        require(ok and self.app_owned(app.get('result', {})), 'Application association rejected, ambiguous or not owned')
        _, sub, ok = self.call('endpoint-enable', self.path + '/subdomain', 'POST',
                               data={'enabled': True, 'previews_enabled': False})
        require(ok and sub['result'].get('enabled') is True and sub['result'].get('previews_enabled') is False,
                'Endpoint enablement rejected or ambiguous')
        event, _, _ = self.call('unauthenticated-404', '/probe', 'POST', host=host)
        require(event['curl_exit'] == 0 and event['status'] == 404 and event.get('probe_response') == 'Not found',
                'Unauthenticated guard not demonstrated; authenticated request withheld')
        require(time.time_ns() // 1_000_000 < expiry, 'Endpoint bearer expired before use')
        self.state['authenticated_calls'] = 1
        self.save()
        event, _, _ = self.call('authenticated-probe', '/probe', 'POST', host=host, bearer=bearer)
        result = event.get('probe_response')
        self.state['runtime'] = result
        self.state['runtime_passed'] = (
            event['curl_exit'] == 0 and event['status'] == 200 and isinstance(result, dict)
            and result.get('stage') == 'greeting'
            and all(result.get(field) is True for field in ('registered', 'tcpConnected', 'sshGreeting', 'containerStopped'))
        )
        self.state['result'] = ('SSH greeting observed and container stopped' if self.state['runtime_passed']
                                else 'Runtime probe did not prove an SSH greeting and container shutdown')

    def cleanup_app(self):
        event, body, ok = self.call('app-cleanup-ownership', self.app_path)
        if ok and self.app_owned(body.get('result', {})):
            self.call('app-delete', self.app_path, 'DELETE')
        event, _, _ = self.call('app-cleanup-id-readback', self.app_path)
        absent_by_id = event['curl_exit'] == 0 and event['status'] == 404
        absent_by_name = self.apps_absent('app-cleanup-name-readback')
        self.state['app_absent_after'] = absent_by_id and absent_by_name

    def cleanup_worker(self):
        absent, owned, _ = self.readback_worker('worker-cleanup-readback')
        if not absent and owned:
            self.call('worker-delete', self.path, 'DELETE')
            absent, _, _ = self.readback_worker('worker-cleanup-absence')
        self.state['worker_absent_after'] = absent


def interrupted(signum, frame):
    raise Stop('Interrupted; only ownership-verified retirement allowed')


def main():
    modes = {'--authorized-upload-only': 'upload', '--authorized-runtime-probe': 'runtime'}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        sys.exit('No action. Use --authorized-upload-only or --authorized-runtime-probe for one authorized attempt.')
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    attempt = Attempt(modes[sys.argv[1]])
    try:
        attempt.run()
    except Stop as error:
        attempt.state['result'] = str(error)
    except (Exception, KeyboardInterrupt) as error:
        attempt.state['result'] = 'Safety or transport stop: ' + type(error).__name__
    finally:
        cleanup = []
        if attempt.state.get('app_create_calls'):
            cleanup.append(attempt.cleanup_app)
        if attempt.state['upload_calls']:
            cleanup.append(attempt.cleanup_worker)
        for action in cleanup:
            try:
                action()
            except (Exception, KeyboardInterrupt) as error:
                attempt.state[action.__name__ + '_error'] = type(error).__name__
        attempt.state['finished_at'] = now()
        attempt.save()
        print(attempt.folder)


if __name__ == '__main__':
    main()
