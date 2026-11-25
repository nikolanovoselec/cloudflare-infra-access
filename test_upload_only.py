import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ACCOUNT_ID = '0123456789abcdef0123456789abcdef'
ACCOUNT_API = f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}'
API_TOKEN = 'fixture-token'
APP_ID = '00000000-0000-4000-8000-000000000001'
VERSION = '00000000-0000-4000-8000-000000000002'
TARGET = '00000000-0000-4000-8000-000000000003'
ACCESS = '00000000-0000-4000-8000-000000000005'
SUBDOMAIN = 'fixture-workers'
RUNTIME_CONFIG = {'identity': 'tester@example.invalid', 'ip': '192.168.250.10',
                  'target_id': TARGET, 'network_id': '00000000-0000-4000-8000-000000000004',
                  'access_app_id': ACCESS, 'policy_id': '00000000-0000-4000-8000-000000000006',
                  'target_name': 'fixture-ssh', 'ssh_username': 'tester'}
PREFLIGHT = {
    f'/infrastructure/targets/{TARGET}': {
        'id': TARGET, 'hostname': 'fixture-ssh', 'target_type': 'ip',
        'ip': {'ipv4': {'ip_addr': '192.168.250.10', 'virtual_network_id': '00000000-0000-4000-8000-000000000004'}},
    },
    f'/access/apps/{ACCESS}': {
        'id': ACCESS, 'type': 'infrastructure', 'target_criteria': [
            {'include': {'target_attributes': {'hostname': ['fixture-ssh']}}, 'port': 22, 'protocol': 'SSH'},
        ],
    },
    f'/access/apps/{ACCESS}/policies': [{
        'id': '00000000-0000-4000-8000-000000000006', 'decision': 'allow',
        'require': [{'email': {'email': 'tester@example.invalid'}}],
        'include': [{'device_posture': {'integration_uid': 'fixture-posture'}}], 'exclude': [],
        'connection_rules': {'ssh': {'usernames': ['tester'], 'allow_email_alias': False}},
    }],
    '/workers/subdomain': {'subdomain': SUBDOMAIN},
}
RUNTIME_RESULT = {'stage': 'greeting', 'registered': True, 'tcpConnected': True,
                  'sshGreeting': True, 'containerStopped': True}
APPS = ACCOUNT_API + '/containers/applications'
FAKE_CURL = f'APP_ID, VERSION, SUBDOMAIN = {APP_ID!r}, {VERSION!r}, {SUBDOMAIN!r}\n' + '''
import json
import os
from pathlib import Path
import sys
import time


def success(result):
    return 200, {'success': True, 'result': result}


args = sys.argv[1:]
method, url = args[args.index('--request') + 1], args[-1]
record = {'method': method, 'url': url}
if '--data-binary' in args:
    record['data'] = json.loads(args[args.index('--data-binary') + 1])
headers = [json.loads(line.removeprefix('header = ')) for line in sys.stdin.read().splitlines()
           if line.startswith('header = ')]
authorization = next((header for header in headers if header.lower().startswith('authorization:')), None)
worker = Path('worker.json')
app = Path('app.json')
endpoint = Path('endpoint.json')
status, body = 500, {'success': False}
api = os.environ['FIXTURE_API']
preflight = json.loads(os.environ['FIXTURE_PREFLIGHT'])
apps = api + '/containers/applications'
prefix = api + '/workers/scripts/'
api_path = url.removeprefix(api)
if method == 'GET' and url.startswith(api + '/') and api_path in preflight:
    status, body = success(preflight[api_path])
elif url.startswith(prefix):
    name, _, resource = url[len(prefix):].partition('/')
    if method == 'GET' and resource == 'settings':
        if worker.exists():
            settings = json.loads(worker.read_text())
            if os.environ['FIXTURE_VERSION_BINDINGS'] == '1':
                del settings['bindings']
            status, body = success(settings)
        else:
            status, body = 404, {'success': False, 'errors': [{'code': 10007}]}
    elif method == 'GET' and resource == 'versions/' + VERSION and worker.exists():
        status, body = success({'id': VERSION, 'resources': {'bindings': json.loads(worker.read_text())['bindings']}})
    elif method == 'PUT' and not resource:
        form = next(arg for arg in args if arg.startswith('metadata=<'))
        metadata = json.loads(Path(form.removeprefix('metadata=<').split(';')[0]).read_text())
        record.update(metadata=metadata, at_ms=time.time_ns() // 1_000_000)
        if os.environ['FIXTURE_ADMIT'] == '1':
            settings = {key: metadata[key] for key in ('annotations', 'tags')}
            settings['bindings'] = [dict(binding, namespace_id=APP_ID, script_name=name)
                                    if binding['type'] == 'durable_object_namespace' else dict(binding)
                                    for binding in metadata['bindings']]
            worker.write_text(json.dumps(settings))
            endpoint.write_text(json.dumps({'enabled': os.environ['FIXTURE_ENDPOINT_ENABLED'] == '1',
                                            'previews_enabled': False}))
            status, body = success({'id': name, 'deployment_id': VERSION})
        else:
            status, body = 401, {'success': False, 'errors': [{'code': 10015}]}
    elif resource == 'subdomain' and worker.exists():
        if method == 'POST':
            endpoint.write_text(json.dumps(record['data']))
        if method in {'GET', 'POST'}:
            status, body = success(json.loads(endpoint.read_text()))
    elif method == 'DELETE' and not resource and worker.exists():
        worker.unlink()
        endpoint.unlink()
        status, body = success({})
elif method == 'GET' and url.startswith(apps + '?name='):
    stored = json.loads(app.read_text()) if app.exists() else None
    status, body = success([stored] if stored and url == apps + '?name=' + stored['name'] else [])
elif method == 'POST' and url == apps:
    data = record['data']
    stored = dict(data, id=data['durable_objects']['namespace_id'], account_id=api.rsplit('/', 1)[1])
    app.write_text(json.dumps(stored))
    status, body = success(stored)
elif url == apps + '/' + APP_ID:
    if method == 'GET':
        status, body = success(json.loads(app.read_text())) if app.exists() else (404, {'success': False})
    elif method == 'DELETE' and app.exists():
        app.unlink()
        status, body = success({})
elif method == 'POST' and worker.exists() and app.exists() and json.loads(endpoint.read_text())['enabled']:
    name = json.loads(app.read_text())['name']
    if url == f'https://{name}.{SUBDOMAIN}.workers.dev/probe':
        record['authorization'] = authorization
        if authorization is None:
            status, body = 404, 'Not found' if os.environ['FIXTURE_DENY_GUARD'] == '1' else 'Different 404'
        else:
            status, body = 200, json.loads(os.environ['FIXTURE_RUNTIME_RESULT'])
            body['debug'] = authorization + ' ' + api
record['status'] = status
with Path('requests.jsonl').open('a') as log:
    print(json.dumps(record), file=log)
raw = body if isinstance(body, str) else json.dumps(body)
print(raw + '\\n' + str(status) + '\\n00000000-ABC\\nMon, 01 Jan 2024 00:00:00 GMT', end='')
'''


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class RunnerTestCase(unittest.TestCase):
    def invoke(self, *, admitted=False, account_id=ACCOUNT_ID, mode='upload-only',
               version_bindings=False, deny_guard=True, endpoint_enabled=False, runtime_config=RUNTIME_CONFIG):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('run_once.py', 'worker.mjs', 'probe.mjs', 'greeting.mjs'):
                shutil.copyfile(Path(__file__).with_name(name), root / name)
            curl = root / 'curl'
            curl.write_text(f'#!{sys.executable}\n' + FAKE_CURL)
            curl.chmod(0o700)
            env = {'PATH': str(root), 'CLOUDFLARE_API_TOKEN': API_TOKEN,
                   'FIXTURE_API': ACCOUNT_API, 'FIXTURE_ADMIT': str(int(admitted)),
                   'FIXTURE_PREFLIGHT': json.dumps(PREFLIGHT), 'FIXTURE_RUNTIME_RESULT': json.dumps(RUNTIME_RESULT),
                   'FIXTURE_VERSION_BINDINGS': str(int(version_bindings)), 'FIXTURE_DENY_GUARD': str(int(deny_guard)),
                   'FIXTURE_ENDPOINT_ENABLED': str(int(endpoint_enabled))}
            if mode == 'runtime-probe' and runtime_config is not None:
                env['CLOUDFLARE_RUNTIME_CONFIG'] = json.dumps(runtime_config)
            if account_id is not None:
                env['CLOUDFLARE_ACCOUNT_ID'] = account_id
            run = subprocess.run(
                [sys.executable, 'run_once.py', '--authorized-' + mode], cwd=root,
                env=env, text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            attempt = Path(run.stdout.strip())
            summary = json.loads((attempt / 'summary.safe.json').read_text())
            requests_file = root / 'requests.jsonl'
            events_file = attempt / 'http.safe.jsonl'
            requests = read_jsonl(requests_file) if requests_file.exists() else []
            events = read_jsonl(events_file) if events_file.exists() else []
            evidence = '\n'.join(file.read_text() for file in attempt.glob('*.safe.*')) + run.stdout + run.stderr
            sensitive = [API_TOKEN, account_id, *(runtime_config or {}).values()]
            for req in requests:
                if authorization := req.get('authorization'):
                    sensitive.append(authorization.rsplit(' ', 1)[-1])
                for binding in req.get('metadata', {}).get('bindings', []):
                    if binding['name'] in {'EXPIRES_AT', 'PROBE_TOKEN_HASH', 'PROBE_IDENTITY', 'PROBE_IP'}:
                        sensitive.append(binding['text'])
            for value in filter(None, sensitive):
                self.assertNotIn(value, evidence)
            return summary, requests, events

    def assert_upload_metadata(self, summary, requests, *, runtime=False):
        # Explicit authorization permits exactly one upload, even when the API rejects it.
        uploads = [req for req in requests if req['method'] == 'PUT']
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        metadata = upload['metadata']
        do_class = 'AlphaProbeContainer'
        bindings = [
            {'name': 'GATEWAY_IDENTITY', 'type': 'vpc_network', 'network_id': 'cf1:network',
             'identity': 'runtime-email-alpha'},
            {'name': 'PROBE', 'type': 'durable_object_namespace', 'class_name': do_class},
        ]
        token_hash = None
        if runtime:
            expiry = next(binding['text'] for binding in metadata['bindings'] if binding['name'] == 'EXPIRES_AT')
            token_hash = next(binding['text'] for binding in metadata['bindings'] if binding['name'] == 'PROBE_TOKEN_HASH')
            self.assertLess(upload['at_ms'], int(expiry))
            self.assertLessEqual(int(expiry), upload['at_ms'] + 300_000)
            self.assertRegex(token_hash, r'^[0-9a-f]{64}$')
            bindings.extend([
                {'name': 'EXPIRES_AT', 'type': 'plain_text', 'text': expiry},
                {'name': 'PROBE_TOKEN_HASH', 'type': 'secret_text', 'text': token_hash},
                {'name': 'PROBE_IDENTITY', 'type': 'secret_text', 'text': RUNTIME_CONFIG['identity']},
                {'name': 'PROBE_IP', 'type': 'secret_text', 'text': RUNTIME_CONFIG['ip']},
            ])
        self.assertCountEqual(metadata['bindings'], bindings)
        self.assertEqual(metadata['containers'], [{'name': summary['name'], 'class_name': do_class}])
        self.assertIn({'new_sqlite_classes': [do_class]}, metadata['migrations']['steps'])
        return token_hash


class UploadOnlyTest(RunnerTestCase):
    def test_missing_or_invalid_account_stops_before_network(self):
        for account_id in (None, '../wrong-account'):
            with self.subTest(account_id=account_id):
                summary, requests, _ = self.invoke(account_id=account_id)
                self.assertEqual(summary['upload_calls'], 0)
                self.assertIn('CLOUDFLARE_ACCOUNT_ID', summary['result'])
                self.assertEqual(requests, [])

    def run_attempt(self, admitted):
        summary, requests, events = self.invoke(admitted=admitted)
        self.assertIs(summary['upload_admitted'], admitted)
        self.assertIs(summary['worker_absent_after'], True)
        self.assertIsNone(summary.get('runtime'))
        script = ACCOUNT_API + '/workers/scripts/' + summary['name']
        allowed = {('GET', script + '/settings'), ('GET', script + '/subdomain'), ('PUT', script)}
        if admitted:
            allowed.add(('DELETE', script))
        for req in requests:
            self.assertIn((req['method'], req['url']), allowed)
        for req in (requests[0], requests[-1]):
            self.assertEqual((req['method'], req['url'], req['status']), ('GET', script + '/settings', 404))

        self.assert_upload_metadata(summary, requests)
        return requests, events

    def test_admission_retires_owned_worker_then_verifies_absence(self):
        requests, _ = self.run_attempt(admitted=True)
        delete = next((i for i, req in enumerate(requests) if req['method'] == 'DELETE'), None)
        self.assertIsNotNone(delete, requests)
        self.assertEqual(requests[delete]['status'], 200)
        self.assertTrue(any(req['url'].endswith('/settings') and req['status'] == 200
                            for req in requests[:delete]), requests)

    def test_rejection_records_401_10015_and_leaves_absent_worker_untouched(self):
        _, events = self.run_attempt(admitted=False)
        upload = next(event for event in events if event['method'] == 'PUT')
        self.assertEqual(upload['status'], 401)
        self.assertIn(10015, [error['code'] for error in upload['errors']])


class RuntimeProbeTest(RunnerTestCase):
    def test_missing_or_invalid_runtime_config_stops_before_network(self):
        for config in (None, dict(RUNTIME_CONFIG, target_id='../invalid'), dict(RUNTIME_CONFIG, ip='127.0.0.1')):
            with self.subTest(config=config):
                summary, requests, _ = self.invoke(mode='runtime-probe', runtime_config=config)
                self.assertEqual(summary['upload_calls'], 0)
                self.assertIn('CLOUDFLARE_RUNTIME_CONFIG', summary['result'])
                self.assertEqual(requests, [])

    def assert_cleanup(self, summary, requests):
        self.assertIs(summary['worker_absent_after'], True)
        self.assertIs(summary['app_absent_after'], True)
        script = ACCOUNT_API + '/workers/scripts/' + summary['name']
        for url, lookup in ((APPS + '/' + APP_ID, APPS + '/' + APP_ID), (script, script + '/settings')):
            trace = [(req['method'], req['status']) for req in requests if req['url'] in {url, lookup}]
            deletion = trace.index(('DELETE', 200))
            self.assertIn(('GET', 200), trace[:deletion])
            self.assertIn(('GET', 404), trace[deletion + 1:])

    def probe_requests(self, summary, requests):
        url = f"https://{summary['name']}.{SUBDOMAIN}.workers.dev/probe"
        probes = [req for req in requests if not req['url'].startswith(ACCOUNT_API + '/')]
        for req in probes:
            self.assertEqual((req['method'], req['url']), ('POST', url))
        return [(req['status'], req['authorization']) for req in probes]

    def test_runtime_requires_denial_before_authenticated_probe_and_retires_owned_resources(self):
        for version_bindings in (False, True):
            with self.subTest(version_bindings=version_bindings):
                summary, requests, _ = self.invoke(mode='runtime-probe', admitted=True,
                                                   version_bindings=version_bindings)
                self.assertEqual(summary['mode'], 'runtime')
                self.assertIs(summary['upload_admitted'], True)
                self.assertIs(summary['runtime_passed'], True)
                self.assertEqual(summary['runtime'], RUNTIME_RESULT)
                token_hash = self.assert_upload_metadata(summary, requests, runtime=True)
                script = ACCOUNT_API + '/workers/scripts/' + summary['name']
                subdomain = script + '/subdomain'
                calls = [(req['method'], req['url']) for req in requests]
                upload = calls.index(('PUT', script))
                for path in PREFLIGHT:
                    self.assertIn(('GET', ACCOUNT_API + path), calls[:upload])
                self.assertNotIn(('GET', ACCOUNT_API), calls)
                self.assertEqual(('GET', script + '/versions/' + VERSION) in calls, version_bindings)
                app_lookup = calls.index(('GET', APPS + '?name=' + summary['name']))
                self.assertIn(('GET', subdomain), calls[upload + 1:app_lookup])
                create, = [req for req in requests if (req['method'], req['url']) == ('POST', APPS)]
                self.assertEqual(create['data'], {'name': summary['name'], 'scheduling_policy': 'durable_object',
                                                  'durable_objects': {'namespace_id': APP_ID}})
                enable, = [req for req in requests if (req['method'], req['url']) == ('POST', subdomain)]
                self.assertEqual(enable['data'], {'enabled': True, 'previews_enabled': False})
                self.assertLess(requests.index(create), requests.index(enable))
                denied, authenticated = self.probe_requests(summary, requests)
                self.assertEqual(denied, (404, None))
                status, header = authenticated
                self.assertEqual(status, 200)
                self.assertRegex(header, r'^Authorization: Bearer [0-9a-f]{64}$')
                self.assertEqual(hashlib.sha256(header.rsplit(' ', 1)[-1].encode()).hexdigest(), token_hash)
                self.assert_cleanup(summary, requests)

    def test_runtime_upload_rejection_never_calls_containers_or_probe(self):
        summary, requests, events = self.invoke(mode='runtime-probe')
        self.assertEqual(summary['mode'], 'runtime')
        self.assertIs(summary['upload_admitted'], False)
        self.assertIs(summary['runtime_passed'], False)
        self.assertIsNone(summary.get('runtime'))
        self.assertIs(summary['worker_absent_after'], True)
        self.assert_upload_metadata(summary, requests, runtime=True)
        script = ACCOUNT_API + '/workers/scripts/' + summary['name']
        allowed = {('GET', ACCOUNT_API + path) for path in PREFLIGHT}
        allowed |= {('GET', script + '/settings'), ('PUT', script)}
        for req in requests:
            self.assertIn((req['method'], req['url']), allowed)
        upload = next(event for event in events if event['method'] == 'PUT')
        self.assertEqual(upload['status'], 401)
        self.assertIn(10015, [error['code'] for error in upload['errors']])

    def test_enabled_endpoint_after_upload_stops_before_container_setup(self):
        summary, requests, _ = self.invoke(mode='runtime-probe', admitted=True, endpoint_enabled=True)
        self.assertIs(summary['upload_admitted'], True)
        self.assertIs(summary['runtime_passed'], False)
        self.assertIs(summary['worker_absent_after'], True)
        self.assertEqual(self.probe_requests(summary, requests), [])
        for req in requests:
            self.assertFalse(req['url'].startswith(APPS), req)
            self.assertNotEqual(req['method'], 'POST')

    def test_unexpected_404_withholds_bearer_but_still_retires_owned_resources(self):
        summary, requests, _ = self.invoke(mode='runtime-probe', admitted=True, deny_guard=False)
        self.assertIs(summary['upload_admitted'], True)
        self.assertIs(summary['runtime_passed'], False)
        self.assertIsNone(summary.get('runtime'))
        self.assertEqual(self.probe_requests(summary, requests), [(404, None)])
        self.assert_cleanup(summary, requests)


if __name__ == '__main__':
    unittest.main()
