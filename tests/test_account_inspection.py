import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from core import account_inspection_service as service, db, plan_check_service as plan_service
from webui.app import create_app


def jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'header.{body}.signature'


def completed(ok, plan_ok=None):
    future = Future()
    future.set_result({'ok': ok, 'plan_result': {'ok': ok if plan_ok is None else plan_ok}})
    return {'accepted': True, 'future': future}


class AccountInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'accounts.json').write_text(json.dumps([
            {'id': 1, 'email': 'valid@example.test', 'access_token': jwt({'exp': 9999999999})},
            {'id': 2, 'email': 'expired@example.test', 'access_token': jwt({'exp': 1})},
            {'id': 3, 'email': 'missing@example.test'},
            {'id': 4, 'email': 'malformed@example.test', 'access_token': 'bad'},
            {'id': 5, 'email': 'archived@example.test', 'archived': True},
        ]))
        paths = {name: root / (name + '.missing') for name in (
            '_OUTLOOK_JSON', '_GENERIC_API_EMAIL_JSON', '_DOMAIN_EMAIL_JSON', '_JOBS_JSON',
            '_LEGACY_ACCOUNTS_JSON', '_LEGACY_OUTLOOK_JSON', '_LEGACY_JOBS_JSON',
            '_LEGACY_SQLITE', '_LEGACY_CODEX_EXPORT_STATE', '_CODEX_DIR', '_CODEX_AGENT_DIR',
            '_TOKENS_TXT', '_ACCOUNTS_TXT')}
        paths.update(_ACCOUNTS_JSON=root / 'accounts.json', _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        self.patches = patch.multiple(db, **paths)
        self.patches.start()
        self.addCleanup(self.patches.stop)
        # 测试中 mock 的 executor 不执行 finally，显式跟踪并释放真实进程锁。
        acquire = service.acquire_inspection_lease
        def tracked_acquire():
            lease = acquire()
            if lease is not None:
                self.addCleanup(lease.close)
            return lease
        lease_patch = patch.object(service, 'acquire_inspection_lease', side_effect=tracked_acquire)
        lease_patch.start()
        self.addCleanup(lease_patch.stop)

    def test_deactivated_is_skipped_before_token_check_and_saved_in_details(self):
        db._ensure_sqlite()
        with closing(db._sqlite_conn()) as conn, conn:
            for account_id in (1, 2, 5):
                conn.execute("UPDATE accounts SET payload=json_set(payload, '$.live_check_status', 'deactivated', "
                             "'$.live_check_error', 'account_deactivated') WHERE id=?", (account_id,))
        run, _ = db.create_account_inspection([1, 2, 3, 5], workers=4, archived='all')
        original_token_check = service.token_needs_refresh
        with patch.object(service, 'token_needs_refresh', wraps=original_token_check) as token_check, \
             patch.object(service.live_check_service, 'enqueue_account_live_check', return_value=completed(True)) as live:
            service._run(run)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['checked_count'], 1)
        self.assertEqual(run['skipped_count'], 3)
        self.assertEqual(run['deactivated_skipped_count'], 3)
        self.assertEqual(run['invalid_count'], 1)
        self.assertEqual(run['live_failed_count'], 0)
        self.assertEqual(token_check.call_count, 1)
        self.assertEqual(live.call_count, 1)
        self.assertEqual(live.call_args.kwargs['account_id'], 3)
        details = db.list_account_inspection_failures(run['id'])
        self.assertEqual(details['total'], 3)
        self.assertEqual({item['status'] for item in details['items']}, {'deactivated_skipped'})
        self.assertTrue(all('account_deactivated' in item['error'] for item in details['items']))
        db.delete_deactivated_accounts('all')
        self.assertEqual(db.list_account_inspection_failures(run['id'])['items'], details['items'])

    def test_delete_deactivated_is_scoped_targeted_and_cleans_agent_credentials(self):
        db._ensure_sqlite()
        with closing(db._sqlite_conn()) as conn, conn:
            for account_id in (1, 2, 5):
                conn.execute("UPDATE accounts SET payload=json_set(payload, '$.live_check_status', 'deactivated') WHERE id=?", (account_id,))
                conn.execute("INSERT INTO codex_agent_accounts(account_id,payload) VALUES (?, '{}')", (account_id,))
            # 恢复正常的账号不能因曾经已废而被删除。
            conn.execute("UPDATE accounts SET payload=json_set(payload, '$.live_check_status', 'ok') WHERE id=1")
            unchanged = conn.execute('SELECT payload FROM accounts WHERE id=1').fetchone()[0]
        with patch.object(db, '_load_accounts', side_effect=AssertionError('must not load all accounts')), \
             patch.object(db, '_save_accounts', side_effect=AssertionError('must not rewrite accounts')):
            self.assertEqual(db.delete_deactivated_accounts('0'), 1)
            self.assertIsNone(db.get_account(2))
            self.assertIsNotNone(db.get_account(5))
            self.assertEqual(db.delete_deactivated_accounts('only'), 1)
            self.assertEqual(db.delete_deactivated_accounts('all'), 0)
        with closing(db._sqlite_conn()) as conn:
            self.assertEqual(conn.execute('SELECT payload FROM accounts WHERE id=1').fetchone()[0], unchanged)
            self.assertEqual([r[0] for r in conn.execute('SELECT account_id FROM codex_agent_accounts')], [1])
        with self.assertRaises(ValueError):
            db.delete_deactivated_accounts('invalid')

    def test_delete_deactivated_transaction_rolls_back_credentials_on_delete_failure(self):
        db._ensure_sqlite()
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("UPDATE accounts SET payload=json_set(payload, '$.live_check_status', 'deactivated') WHERE id=2")
            conn.execute("INSERT INTO codex_agent_accounts(account_id,payload) VALUES (2, '{}')")
            conn.executescript("CREATE TRIGGER reject_account_delete BEFORE DELETE ON accounts "
                               "BEGIN SELECT RAISE(ABORT, 'delete failed'); END;")
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'delete failed'):
            db.delete_deactivated_accounts('all')
        self.assertIsNotNone(db.get_account(2))
        with closing(db._sqlite_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM codex_agent_accounts WHERE account_id=2').fetchone()[0], 1)

    def test_delete_deactivated_waits_for_writer_and_rechecks_committed_status(self):
        db._ensure_sqlite()
        db.update_account_liveness(2, {'ok': False, 'status': 'deactivated', 'error': 'old failure'})
        # 外部连接代表另一个进程先恢复正常但尚未提交的账号状态。
        original_conn = db._sqlite_conn
        deleting = threading.Event()
        def traced_conn():
            conn = original_conn()
            conn.set_trace_callback(lambda sql: deleting.set() if sql == 'BEGIN IMMEDIATE' else None)
            return conn
        with closing(original_conn()) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE accounts SET payload=json_set(payload, '$.live_check_status', 'ok') WHERE id=2")
            with ThreadPoolExecutor(max_workers=1) as pool, patch.object(db, '_sqlite_conn', side_effect=traced_conn):
                future = pool.submit(db.delete_deactivated_accounts, 'all')
                try:
                    self.assertTrue(deleting.wait(5))
                    self.assertFalse(future.done())
                finally:
                    writer.commit()
                self.assertEqual(future.result(timeout=5), 0)
        self.assertEqual(db.get_account(2)['live_check_status'], 'ok')

    def test_deactivated_skip_detail_failure_rolls_back_both_skip_counters(self):
        db._ensure_sqlite()
        db.update_account_liveness(2, {'ok': False, 'status': 'deactivated', 'error': 'account_deactivated'})
        run, _ = db.create_account_inspection([2], workers=1)
        with closing(db._sqlite_conn()) as conn, conn:
            conn.executescript("CREATE TRIGGER fail_skip_detail BEFORE INSERT ON account_inspection_failures "
                               "BEGIN SELECT RAISE(ABORT, 'skip detail failure'); END;")
        with patch.object(service.live_check_service, 'enqueue_account_live_check') as live, \
             patch.object(service.logger, 'exception'):
            service._run(run)
        live.assert_not_called()
        details = db.list_account_inspection_failures(run['id'])
        self.assertEqual(details['task']['status'], 'failed')
        self.assertEqual(details['task']['skipped_count'], 0)
        self.assertEqual(details['task']['deactivated_skipped_count'], 0)
        self.assertEqual(details['total'], 0)

    def test_delete_deactivated_api_validates_scope_and_auth(self):
        client = create_app(auth_code='delete-test').test_client()
        headers = {'X-Auth-Code': 'delete-test', 'Accept-Encoding': 'identity'}
        url = '/api/accounts/delete-deactivated'
        self.assertEqual(client.post(url, json={'archived':'all'}).status_code, 401)
        for data in ({}, [], {'archived':True}, {'archived':[]}, {'archived':'bad'}):
            self.assertEqual(client.post(url, json=data, headers=headers).status_code, 400)
        self.assertEqual(client.post(url, json={'archived':'only'}, headers=headers).get_json()['deleted_count'], 0)
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("UPDATE accounts SET payload=json_set(payload, '$.live_check_status', 'deactivated') WHERE id IN (2,5)")
        self.assertEqual(client.post(url, json={'archived':'only'}, headers=headers).get_json()['deleted_count'], 1)
        self.assertIsNone(db.get_account(5))
        self.assertIsNotNone(db.get_account(2))
        self.assertEqual(client.post(url, json={'archived':'all'}, headers=headers).get_json()['deleted_count'], 1)
        self.assertIsNotNone(db.get_account(1))


    def test_live_failure_details_capture_rejection_exception_and_account_snapshot(self):
        run, _ = db.create_account_inspection([2, 3, 4], workers=1)
        def enqueue(**kwargs):
            if kwargs['account_id'] == 2:
                return {'accepted': False, 'error': '该账号正在查活'}
            if kwargs['account_id'] == 3:
                raise RuntimeError('mailbox unavailable')
            future = Future()
            future.set_result({'ok': False, 'status': 'deactivated', 'error': 'account_deactivated'})
            return {'accepted': True, 'future': future}
        with patch.object(service.live_check_service, 'enqueue_account_live_check', side_effect=enqueue), \
             patch.object(service.logger, 'exception'):
            service._run(run)
        details = db.list_account_inspection_failures(run['id'])
        self.assertTrue(details['details_available'])
        self.assertEqual(details['total'], run['live_failed_count'])
        self.assertEqual(details['total'], 3)
        records = {r['account_id']: r for r in details['items']}
        self.assertEqual(records[2]['status'], 'rejected')
        self.assertEqual(records[3]['error'], 'RuntimeError: mailbox unavailable')
        self.assertEqual(records[4]['status'], 'deactivated')
        self.assertTrue(all(r['failed_at'] for r in records.values()))
        db.update_account_liveness(2, {'ok': True, 'access_token': 'new-token'})
        db.delete_accounts([3, 4])
        db._SQLITE_READY = False
        db._ensure_sqlite()
        self.assertEqual(db.list_account_inspection_failures(run['id'])['items'], details['items'])
        self.assertNotIn('access_token', str(details))
        self.assertNotIn('account_ids', details['task'])

    def test_failure_detail_and_statistics_roll_back_together_and_terminal_is_immutable(self):
        run, _ = db.create_account_inspection([2])
        failure = {'account_id': 2, 'email': 'old@example.test', 'error': 'test failure'}
        with closing(db._sqlite_conn()) as conn, conn:
            conn.executescript("""
                CREATE TRIGGER fail_failure_detail BEFORE INSERT ON account_inspection_failures
                BEGIN SELECT RAISE(ABORT, 'detail storage failure'); END;
            """)
        run.update(status='running', checked_count=1, invalid_count=1, live_failed_count=1)
        with self.assertRaisesRegex(Exception, 'detail storage failure'):
            db.save_account_inspection(run, failure=failure)
        details = db.list_account_inspection_failures(run['id'])
        self.assertEqual(details['total'], 0)
        self.assertEqual(details['task']['live_failed_count'], 0)
        self.assertEqual(details['task']['status'], 'queued')
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute('DROP TRIGGER fail_failure_detail')
        self.assertTrue(db.save_account_inspection(run, failure=failure))
        self.assertTrue(db.save_account_inspection(run, failure=failure))
        self.assertEqual(db.list_account_inspection_failures(run['id'])['total'], 1)
        run['status'] = 'completed'
        db.save_account_inspection(run)
        self.assertFalse(db.save_account_inspection(run, failure={'account_id': 3, 'error': 'late result'}))
        self.assertEqual(db.list_account_inspection_failures(run['id'])['total'], 1)

    def test_detail_write_failure_does_not_leave_unmatched_count_in_final_save(self):
        run, _ = db.create_account_inspection([2])
        with closing(db._sqlite_conn()) as conn, conn:
            conn.executescript("""
                CREATE TRIGGER fail_failure_detail BEFORE INSERT ON account_inspection_failures
                BEGIN SELECT RAISE(ABORT, 'detail storage failure'); END;
            """)
        with patch.object(service.live_check_service, 'enqueue_account_live_check', return_value=completed(False)), \
             patch.object(service.logger, 'exception'):
            service._run(run)
        result = db.list_account_inspection_failures(run['id'])
        self.assertEqual(result['task']['status'], 'failed')
        self.assertIn('detail storage failure', result['task']['error'])
        self.assertEqual(result['task']['live_failed_count'], 0)
        self.assertEqual(result['total'], 0)

    def test_failure_details_api_pagination_auth_and_legacy_history(self):
        app = create_app(auth_code='details-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'details-test', 'Accept-Encoding': 'identity'}
        run, _ = db.create_account_inspection(list(range(1, 26)))
        run['status'] = 'running'
        for i in range(1, 26):
            run.update(checked_count=i, invalid_count=i, live_failed_count=i)
            db.save_account_inspection(run, failure={'account_id': i, 'email': f'{i}@example.test', 'error': f'failure-{i}'})
        url = f"/api/accounts/inspections/{run['id']}/failures"
        self.assertEqual(client.get(url).status_code, 401)
        page1 = client.get(url, headers=headers).get_json()
        self.assertEqual(len(page1['items']), 20)
        self.assertEqual(page1['total'], 25)
        page2 = client.get(url + '?page=2', headers=headers).get_json()
        self.assertEqual([r['account_id'] for r in page2['items']], [21, 22, 23, 24, 25])
        self.assertEqual(client.get(url + '?page=bad', headers=headers).status_code, 400)
        self.assertEqual(client.get(url + '?page=0', headers=headers).status_code, 400)
        self.assertEqual(client.get(url + '?page=' + '9'*100, headers=headers).status_code, 400)
        self.assertEqual(client.get('/api/accounts/inspections/999999/failures', headers=headers).status_code, 404)
        run['status'] = 'completed'
        db.save_account_inspection(run)
        legacy, _ = db.create_account_inspection([2])
        legacy.pop('failure_details_available')
        legacy.update(status='completed', live_failed_count=1)
        db.save_account_inspection(legacy)
        result = db.list_account_inspection_failures(legacy['id'])
        self.assertFalse(result['details_available'])
        self.assertEqual(result['items'], [])

    def test_plan_failure_does_not_create_live_failure_record(self):
        run, _ = db.create_account_inspection([2])
        with patch.object(service.live_check_service, 'enqueue_account_live_check', return_value=completed(True, False)):
            service._run(run)
        self.assertEqual(run['plan_failed_count'], 1)
        self.assertEqual(db.list_account_inspection_failures(run['id'])['total'], 0)

    def test_idle_history_poll_does_not_wait_for_unrelated_writer(self):
        run, _ = db.create_account_inspection([1])
        run.update(status='completed', checked_count=1)
        db.save_account_inspection(run)
        original = db._sqlite_conn
        def short_timeout_connection():
            conn = original()
            conn.execute('PRAGMA busy_timeout=50')
            return conn
        with closing(original()) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(db, '_sqlite_conn', side_effect=short_timeout_connection):
                history = service.list_inspections()
            writer.rollback()
        self.assertIsNone(history['active'])
        self.assertEqual(history['total'], 1)

    def test_connection_setup_failure_closes_connection(self):
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError('pragma setup failed')
        with patch.object(db.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'pragma setup failed'):
                db._sqlite_conn()
        connection.close.assert_called_once()

    def test_schema_initialization_failure_closes_connection(self):
        db._ensure_sqlite()
        class FailingConnection(sqlite3.Connection):
            closed = False
            def execute(self, sql, *args, **kwargs):
                if sql.startswith('CREATE INDEX'):
                    raise OSError('schema initialization failed')
                return super().execute(sql, *args, **kwargs)
            def close(self):
                self.closed = True
                return super().close()
        conn = sqlite3.connect(str(db._active_sqlite_path()), factory=FailingConnection)
        self.addCleanup(conn.close)
        db._SQLITE_READY = False
        with patch.object(db, '_sqlite_conn', return_value=conn):
            with self.assertRaisesRegex(OSError, 'schema initialization failed'):
                db._ensure_sqlite()
        self.assertTrue(conn.closed)
        self.assertFalse(db._SQLITE_READY)

    def test_email_lookup_avoids_full_account_payload_loading(self):
        db._ensure_sqlite()
        with patch.object(db, '_load_accounts', side_effect=AssertionError('email lookup loads all accounts')):
            self.assertEqual(db.get_account_by_email('EXPIRED@EXAMPLE.TEST')['id'], 2)
            self.assertIsNone(db.get_account_by_email('absent@example.test'))

    def test_email_lookup_keeps_first_duplicate_and_empty_email_behavior(self):
        with db._LOCK:
            db._save_accounts([
                {'id': 1, 'email': ''}, {'id': 2, 'email': 'same@example.test'},
                {'id': 3, 'email': 'SAME@example.test'},
            ])
        self.assertEqual(db.get_account_by_email(None)['id'], 1)
        self.assertEqual(db.get_account_by_email('')['id'], 1)
        self.assertEqual(db.get_account_by_email('SAME@example.test')['id'], 2)
        self.assertIsNone(db.get_account_by_email(' same@example.test'))

    def test_email_lookup_and_plan_updates_preserve_unicode_lower_matching(self):
        with db._LOCK:
            db._save_accounts([
                {'id': 1, 'email': 'K@example.test'},
                {'id': 2, 'email': 'k@example.test'},
                {'id': 3, 'email': 'Élodie@example.test'},
            ])
        # Kelvin 符号的小写为 ASCII k；必须仍选择原 Python 匹配语义下 ID 最小者。
        self.assertEqual(db.get_account_by_email('K@EXAMPLE.TEST')['id'], 1)
        self.assertTrue(db.claim_account_plan_check(email='K@EXAMPLE.TEST', run_id='unicode-ascii'))
        self.assertEqual(db.get_account(1)['plan_check_run_id'], 'unicode-ascii')
        self.assertNotIn('plan_check_run_id', db.get_account(2))
        self.assertTrue(db.claim_account_plan_check(email='élodie@example.test', run_id='unicode'))
        self.assertEqual(db.get_account(3)['plan_check_run_id'], 'unicode')
        self.assertTrue(db.update_account_plan_check(email='ÉLODIE@example.test', result={'ok': True, 'current_plan_type': 'plus'}))
        self.assertEqual(db.get_account(3)['current_plan_type'], 'plus')

    def test_snapshot_is_immutable_and_not_rewritten_with_statistics(self):
        ids = list(range(1, 10001))
        run, _ = db.create_account_inspection(ids)
        with closing(db._sqlite_conn()) as conn, conn:
            snapshot = conn.execute('SELECT account_ids FROM account_inspection_snapshots WHERE inspection_id=?', (run['id'],)).fetchone()[0]
            conn.executescript("""
                CREATE TRIGGER immutable_inspection_snapshot BEFORE UPDATE ON account_inspection_snapshots
                BEGIN SELECT RAISE(ABORT, 'snapshot rewritten'); END;
                CREATE TRIGGER bounded_inspection_statistics BEFORE UPDATE ON account_inspections
                WHEN LENGTH(NEW.payload)>2000
                BEGIN SELECT RAISE(ABORT, 'statistics contains full snapshot'); END;
            """)
        run.update(status='running', checked_count=1)
        self.assertTrue(db.save_account_inspection(run))
        with closing(db._sqlite_conn()) as conn:
            stored = conn.execute('SELECT payload FROM account_inspections WHERE id=?', (run['id'],)).fetchone()[0]
            self.assertNotIn('account_ids', json.loads(stored))
            self.assertLess(len(stored), 2000)
            self.assertEqual(conn.execute('SELECT account_ids FROM account_inspection_snapshots WHERE inspection_id=?', (run['id'],)).fetchone()[0], snapshot)
        self.assertNotIn('account_ids', db.list_account_inspections()['active'])
        existing, created = db.create_account_inspection([99999])
        self.assertFalse(created)
        self.assertEqual(existing['account_ids'], ids)
        self.assertEqual(existing['checked_count'], 1)

    def test_legacy_inspection_snapshot_migration_keeps_history_and_recovery(self):
        run, _ = db.create_account_inspection([2, 3])
        run.update(status='running', checked_count=1, invalid_count=1)
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute('UPDATE account_inspections SET status=?,payload=? WHERE id=?', ('running', json.dumps(run), run['id']))
            conn.execute('DELETE FROM account_inspection_snapshots')
            conn.execute("DELETE FROM storage_meta WHERE key='inspection_snapshots_v1'")
        db._SQLITE_READY = False
        db._ensure_sqlite()
        existing, created = db.create_account_inspection([4])
        self.assertFalse(created)
        self.assertEqual(existing['account_ids'], [2, 3])
        self.assertEqual(existing['checked_count'], 1)
        self.assertEqual(db.recover_interrupted_account_inspections(), 1)
        history = db.list_account_inspections()['items'][0]
        self.assertEqual(history['status'], 'interrupted')
        self.assertEqual(history['checked_count'], 1)
        with closing(db._sqlite_conn()) as conn:
            self.assertNotIn('account_ids', json.loads(conn.execute('SELECT payload FROM account_inspections').fetchone()[0]))
        db._SQLITE_READY = False
        db._ensure_sqlite()
        self.assertEqual(db.list_account_inspections()['items'][0], history)

    def test_snapshot_migration_failure_rolls_back_and_releases_connection(self):
        run, _ = db.create_account_inspection([2, 3])
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute('UPDATE account_inspections SET payload=? WHERE id=?', (json.dumps(run), run['id']))
            conn.execute('DELETE FROM account_inspection_snapshots')
            conn.execute("DELETE FROM storage_meta WHERE key='inspection_snapshots_v1'")
            conn.executescript("""
                CREATE TRIGGER fail_snapshot_migration BEFORE INSERT ON account_inspection_snapshots
                BEGIN SELECT RAISE(ABORT, 'migration storage failure'); END;
            """)
        db._SQLITE_READY = False
        with self.assertRaisesRegex(Exception, 'migration storage failure'):
            db._ensure_sqlite()
        self.assertFalse(db._SQLITE_READY)
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute('PRAGMA busy_timeout=1000')
            conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(json.loads(conn.execute('SELECT payload FROM account_inspections').fetchone()[0])['account_ids'], [2, 3])
            self.assertIsNone(conn.execute("SELECT 1 FROM storage_meta WHERE key='inspection_snapshots_v1'").fetchone())
            conn.execute('DROP TRIGGER fail_snapshot_migration')
        db._ensure_sqlite()
        self.assertEqual(db.create_account_inspection([4])[0]['account_ids'], [2, 3])

    def test_snapshot_insert_failure_rolls_back_task_creation(self):
        db._ensure_sqlite()
        with closing(db._sqlite_conn()) as conn, conn:
            conn.executescript("""
                CREATE TRIGGER fail_snapshot BEFORE INSERT ON account_inspection_snapshots
                BEGIN SELECT RAISE(ABORT, 'snapshot storage unavailable'); END;
            """)
        with self.assertRaisesRegex(Exception, 'snapshot storage unavailable'):
            db.create_account_inspection([2, 3])
        self.assertEqual(db.list_account_inspections()['total'], 0)

    def test_worker_failure_stops_other_workers_from_claiming_more_accounts(self):
        run, _ = db.create_account_inspection([2, 3, 4, 5], workers=2)
        entered = threading.Event()
        stopped = threading.Event()
        calls, stop_observed = [], []
        def get(account_id):
            calls.append(account_id)
            if account_id == 2:
                if not entered.wait(3):
                    raise TimeoutError('second worker missing')
                raise OSError('account read failed')
            if account_id == 3:
                entered.set()
                stop_observed.append(stopped.wait(3))
            return {'id': account_id, 'access_token': jwt({'exp': 9999999999})}
        threading_wrapper = Mock(wraps=threading)
        threading_wrapper.Event.return_value = stopped
        with patch.object(service, 'threading', threading_wrapper), \
             patch.object(db, 'get_account', side_effect=get), \
             patch.object(service.logger, 'exception'):
            service._run(run)
        self.assertEqual(stop_observed, [True])
        self.assertEqual(sorted(calls), [2, 3])
        self.assertEqual(run['status'], 'failed')
        self.assertIsNone(db.list_account_inspections()['active'])

    def test_hot_path_reads_and_writes_only_target_account(self):
        db._ensure_sqlite()
        with closing(db._sqlite_conn()) as conn, conn:
            untouched = conn.execute('SELECT payload FROM accounts WHERE id=1').fetchone()[0]
            conn.executescript("""
                CREATE TRIGGER protect_other_update BEFORE UPDATE ON accounts WHEN OLD.id != 2
                BEGIN SELECT RAISE(ABORT, 'unrelated account updated'); END;
                CREATE TRIGGER protect_other_delete BEFORE DELETE ON accounts
                BEGIN SELECT RAISE(ABORT, 'accounts must not be rewritten'); END;
            """)
        with patch.object(db, '_load_accounts', side_effect=AssertionError('full read')), \
             patch.object(db, '_save_accounts', side_effect=AssertionError('full write')):
            self.assertEqual(db.get_account(2)['id'], 2)
            self.assertIsNone(db.get_account(999999))
            self.assertTrue(db.claim_account_live_check(2, run_id='live'))
            self.assertTrue(db.mark_account_live_check_running(2, run_id='live'))
            self.assertTrue(db.update_account_liveness(2, {'ok': True, 'access_token': 'fresh'}, expected_run_id='live'))
            self.assertTrue(db.claim_account_plan_check(2, run_id='plan'))
            self.assertTrue(db.mark_account_plan_check_running(2, run_id='plan'))
            self.assertTrue(db.update_account_plan_check(2, result={'ok': True, 'current_plan_type': 'plus'}, expected_run_id='plan'))
        with closing(db._sqlite_conn()) as conn, conn:
            self.assertEqual(conn.execute('SELECT payload FROM accounts WHERE id=1').fetchone()[0], untouched)
        self.assertEqual(db.get_account(2)['current_plan_type'], 'plus')

    def test_single_account_transaction_rollback_and_index_consistency(self):
        before = db.get_account(2)
        with self.assertRaisesRegex(RuntimeError, 'injected rollback'):
            with db._LOCK, db._account_for_update(2) as row:
                row.update(email='rollback@example.test', access_token='discard')
                raise RuntimeError('injected rollback')
        self.assertEqual(db.get_account(2), before)
        with db._LOCK, db._account_for_update(2) as row:
            row.update(email='changed@example.test', archived=True, status='changed', updated_at='2030-01-01')
        with closing(db._sqlite_conn()) as conn, conn:
            indexed = conn.execute('SELECT email,archived,status,updated_at,payload FROM accounts WHERE id=2').fetchone()
        payload = json.loads(indexed['payload'])
        for field in ('email', 'status', 'updated_at'):
            self.assertEqual(indexed[field], payload[field])
        self.assertEqual(indexed['archived'], 1)
        self.assertEqual(payload['copy_line'], db._account_line(payload))
        self.assertFalse(db.update_account_liveness(999999, {'ok': True, 'access_token': 'must-not-insert'}))
        self.assertIsNone(db.get_account(999999))

    def test_single_account_update_failure_rolls_back_claim(self):
        db._ensure_sqlite()
        with closing(db._sqlite_conn()) as conn, conn:
            conn.executescript("""
                CREATE TRIGGER fail_account_update BEFORE UPDATE ON accounts
                BEGIN SELECT RAISE(ABORT, 'injected write failure'); END;
            """)
        before = db.get_account(2)
        with self.assertRaisesRegex(Exception, 'injected write failure'):
            db.claim_account_live_check(2, run_id='not-committed')
        self.assertEqual(db.get_account(2), before)

    def test_single_account_plan_email_fallback_matches_previous_order(self):
        self.assertTrue(db.claim_account_plan_check(email='EXPIRED@EXAMPLE.TEST', run_id='email'))
        self.assertEqual(db.get_account(2)['plan_check_run_id'], 'email')
        self.assertFalse(db.claim_account_plan_check(email='missing@example.invalid', run_id='none'))
        # 历史 API 同时给 ID 和邮箱时按 ID 升序匹配首行，保留兼容行为。
        self.assertTrue(db.update_account_plan_check(3, email='VALID@EXAMPLE.TEST', result={'ok': True, 'current_plan_type': 'plus'}))
        self.assertEqual(db.get_account(1)['current_plan_type'], 'plus')
        self.assertNotIn('current_plan_type', db.get_account(3))

    def test_expiry_boundaries_and_bad_tokens(self):
        self.assertFalse(service.token_needs_refresh(jwt({'exp': 101}), now=100))
        for token in ['', 'bad', jwt({}), jwt([]), jwt({'exp': True}), jwt({'exp': '101'}),
                      jwt({'exp': 99}), jwt({'exp': 100}), jwt({'exp': float('nan')}), jwt({'exp': float('inf')})]:
            with self.subTest(token=token):
                self.assertTrue(service.token_needs_refresh(token, now=100))

    def test_pipeline_uses_fresh_token_and_counts(self):
        run, _ = db.create_account_inspection([1, 2, 3, 4])
        def live(**kw):
            ok = kw['account_id'] != 3
            if ok:
                db.update_account_liveness(kw['account_id'], {'ok': True, 'access_token': 'fresh-token'})
            return completed(ok, kw['account_id'] == 2)
        with patch.object(service.live_check_service, 'enqueue_account_live_check', side_effect=live) as live_mock, \
             patch.object(plan_service, 'enqueue_account_plan_check', side_effect=AssertionError('巡检不应重复查套餐')) as plan_mock:
            service._run(run)
        self.assertEqual(live_mock.call_count, 3)
        plan_mock.assert_not_called()
        result = db.list_account_inspections()['items'][0]
        self.assertEqual(result['status'], 'completed')
        for key, count in dict(checked_count=4, invalid_count=3, live_success_count=2,
                               live_failed_count=1, plan_success_count=1, plan_failed_count=1).items():
            self.assertEqual(result[key], count)
        self.assertNotIn('account_ids', result)
        self.assertIsNone(db.list_account_inspections()['active'])

    def test_busy_and_exception_do_not_stop_remaining_accounts(self):
        run, _ = db.create_account_inspection([2, 3, 4])
        with patch.object(service.live_check_service, 'enqueue_account_live_check', side_effect=[
            {'accepted': False, 'busy': True}, RuntimeError('test'), completed(True, False)]), \
             patch.object(plan_service, 'enqueue_account_plan_check', return_value={'accepted': False}):
            service._run(run)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['checked_count'], 3)
        self.assertEqual(run['live_failed_count'], 2)
        self.assertEqual(run['live_success_count'], 1)
        self.assertEqual(run['plan_failed_count'], 1)

    def test_real_queue_workers_persist_refreshed_at_and_plan(self):
        run, _ = db.create_account_inspection([1, 2, 3, 4, 2, 3], workers=4)
        self.assertEqual(run['total_count'], 4)
        run['account_ids'].extend([2, 3])  # 执行层也要防御重复 ID。
        barrier = threading.Barrier(4, timeout=10)
        seen = []
        seen_lock = threading.Lock()
        fresh_token = jwt({'exp': 9999999999})
        def live(email, **kwargs):
            with seen_lock:
                seen.append(email)
            # 必须真正同时执行四个查活，才能通过；不能被全局查活池限制为 3。
            barrier.wait()
            return kwargs['on_authenticated']({'ok': True, 'access_token': fresh_token}, sessions[email])
        sessions = {db.get_account(i)['email']: object() for i in (1, 2, 3, 4)}
        def plan_result(token, **kwargs):
            self.assertEqual(token, fresh_token)
            self.assertIn(kwargs['session'], sessions.values())
            return {'ok': True, 'current_plan_type': 'plus'}
        with patch.object(service, 'token_needs_refresh', return_value=True), \
             patch.object(service.live_check_service, 'check_account_liveness', side_effect=live), \
             patch.object(service.live_check_service, '_append_log'), \
             patch.object(service.live_check_service, '_configured_live_check_proxies', return_value=[]), \
             patch.object(service.live_check_service, 'resolve_plan_check_route', return_value={}), \
             patch.object(service.live_check_service, 'open_plan_check_proxy', return_value=(None, None)), \
             patch.object(plan_service, '_wait_for_rate_slot'), \
             patch.object(plan_service, 'check_account_plan',
                          side_effect=plan_result) as plan:
            service._run(run)
        self.assertEqual(run['live_success_count'], 4)
        self.assertEqual(run['plan_success_count'], 4)
        self.assertEqual(run['checked_count'], 4)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(db.get_account(2)['access_token'], fresh_token)
        self.assertEqual(db.get_account(2)['current_plan_type'], 'plus')
        self.assertEqual(plan.call_args.args[0], fresh_token)

    def test_atomic_start_and_recovery_keep_history(self):
        # 先初始化，之后让多个线程竞争同一条唯一索引。
        db.list_account_inspections()
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: db.create_account_inspection([1]), range(6)))
        self.assertEqual(sum(created for _, created in results), 1)
        run = results[0][0]
        run.update(status='running', checked_count=1)
        db.save_account_inspection(run)
        self.assertEqual(db.recover_interrupted_account_inspections(), 1)
        history = db.list_account_inspections()
        self.assertIsNone(history['active'])
        self.assertEqual(history['items'][0]['status'], 'interrupted')
        self.assertEqual(history['items'][0]['checked_count'], 1)
        self.assertTrue(db.create_account_inspection([])[1])
        self.assertEqual(db.list_account_inspections(limit=1, offset=1)['total'], 2)

    def test_start_snapshots_all_unarchived_and_prevents_duplicate(self):
        with patch.object(service._EXECUTOR, 'submit') as submit:
            run, created = service.start_inspection()
            self.assertTrue(created)
            self.assertEqual(run['total_count'], 4)
            second, created = service.start_inspection()
            self.assertFalse(created)
            self.assertEqual(run['id'], second['id'])
            submit.assert_called_once()

    def test_concurrent_statistics_and_exactly_once_across_many_accounts(self):
        run, _ = db.create_account_inspection(list(range(1, 41)) * 2, workers=4)
        barrier = threading.Barrier(4, timeout=10)
        seen = []
        lock = threading.Lock()
        def live(account, executor):
            with lock:
                seen.append(account['id'])
            barrier.wait()
            return {'ok': account['id'] % 2 == 0, 'plan_result': {'ok': account['id'] % 4 == 0}}
        with patch.object(db, 'get_account', side_effect=lambda account_id: {'id': account_id}), \
             patch.object(service, '_check_live', side_effect=live), \
             patch.object(plan_service, 'enqueue_account_plan_check', side_effect=lambda account_id, executor: account_id % 4 == 0):
            service._run(run)
        self.assertEqual(sorted(seen), list(range(1, 41)))
        result = db.list_account_inspections()['items'][0]
        for key, count in dict(total_count=40, checked_count=40, invalid_count=40,
                               live_success_count=20, live_failed_count=20,
                               plan_success_count=10, plan_failed_count=10).items():
            self.assertEqual(result[key], count)
        self.assertEqual(result['workers'], 4)
        self.assertEqual(result['status'], 'completed')

    def test_workers_validation_and_api_persistence(self):
        app = create_app(auth_code='inspection-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        with patch.object(service._EXECUTOR, 'submit') as submit:
            for workers in (0, -1, 33, True, 2.5, '4', None):
                with self.subTest(workers=workers):
                    self.assertEqual(client.post('/api/accounts/inspections', json={'workers': workers}, headers=headers).status_code, 400)
            self.assertEqual(client.post('/api/accounts/inspections', json=[4], headers=headers).status_code, 400)
            submit.assert_not_called()
            response = client.post('/api/accounts/inspections', json={'workers': 8}, headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()['task']['workers'], 8)
            self.assertEqual(submit.call_args.args[1]['workers'], 8)
            self.assertEqual(client.post('/api/accounts/inspections', json={'workers': 2}, headers=headers).status_code, 409)
        self.assertEqual(db.list_account_inspections()['active']['workers'], 8)

    def test_archived_only_snapshot_and_pipeline_preserve_archive_state(self):
        with patch.object(service._EXECUTOR, 'submit') as submit:
            task, created = service.start_inspection(workers=2, archived='only')
            self.assertTrue(created)
            self.assertEqual(task['archived'], 'only')
            self.assertEqual(task['total_count'], 1)
            run = submit.call_args.args[1]
            self.assertEqual(run['account_ids'], [5])
        with patch.object(service, '_check_live', return_value={'ok': True, 'plan_result': {'ok': True}}) as live, \
             patch.object(plan_service, 'enqueue_account_plan_check', return_value=True) as plan:
            service._run(run)
        self.assertEqual(live.call_args.args[0]['id'], 5)
        plan.assert_not_called()
        self.assertTrue(db.get_account(5)['archived'])
        history = db.list_account_inspections()['items'][0]
        self.assertEqual(history['archived'], 'only')
        self.assertEqual(history['checked_count'], 1)
        self.assertEqual(history['status'], 'completed')

    def test_scope_api_validation_persistence_and_global_deduplication(self):
        app = create_app(auth_code='inspection-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        with patch.object(service._EXECUTOR, 'submit') as submit:
            for archived in ('bad', [], {}, None, 1):
                response = client.post('/api/accounts/inspections', json={'archived': archived}, headers=headers)
                self.assertEqual(response.status_code, 400)
            submit.assert_not_called()
            response = client.post('/api/accounts/inspections', json={'archived': 'only', 'workers': 2}, headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()['task']['archived'], 'only')
            self.assertEqual(response.get_json()['task']['total_count'], 1)
            # 不同归档范围也不能绕过全局未完成任务防重。
            duplicate = client.post('/api/accounts/inspections', json={'archived': '0'}, headers=headers)
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(duplicate.get_json()['task']['archived'], 'only')
            submit.assert_called_once()
        history = client.get('/api/accounts/inspections', headers=headers).get_json()
        self.assertEqual(history['active']['archived'], 'only')
        self.assertEqual(history['items'][0]['archived'], 'only')
        db.recover_interrupted_account_inspections()
        self.assertEqual(db.list_account_inspections()['items'][0]['archived'], 'only')

    def test_all_and_default_scope_with_history(self):
        with patch.object(service._EXECUTOR, 'submit') as submit:
            task, _ = service.start_inspection(archived='all')
            self.assertEqual(task['total_count'], 5)
            self.assertEqual(task['archived'], 'all')
            self.assertEqual(set(submit.call_args.args[1]['account_ids']), {1, 2, 3, 4, 5})
            submit.call_args.args[2].close()  # 模拟旧进程退出，OS 释放锁。
            db.recover_interrupted_account_inspections()
            task, _ = service.start_inspection()
            self.assertEqual(task['total_count'], 4)
            self.assertEqual(task['archived'], '0')
            self.assertNotIn(5, submit.call_args.args[1]['account_ids'])
        self.assertEqual([r['archived'] for r in db.list_account_inspections()['items']], ['0', 'all'])

    def test_snapshot_reads_ids_once_without_offset_pagination(self):
        db._save_accounts([{'id': i, 'email': f'{i}@example.test', 'archived': i % 2 == 0}
                           for i in range(1, 1202)])
        with patch.object(db, 'list_accounts', side_effect=AssertionError('不得使用 OFFSET 分页快照')), \
             patch.object(service._EXECUTOR, 'submit') as submit:
            run, created = service.start_inspection(archived='only')
        self.assertTrue(created)
        self.assertEqual(run['total_count'], 600)
        ids = submit.call_args.args[1]['account_ids']
        self.assertEqual(set(ids), set(range(2, 1202, 2)))
        self.assertEqual(len(ids), len(set(ids)))

    def test_database_claim_error_does_not_leak_queue_slots(self):
        for module, claim, enqueue in (
            (service.live_check_service, 'claim_account_live_check', 'enqueue_account_live_check'),
            (plan_service, 'claim_account_plan_check', 'enqueue_account_plan_check'),
        ):
            slots = threading.BoundedSemaphore(1)
            with self.subTest(module=module.__name__), \
                 patch.object(module, '_QUEUE_SLOTS', slots), \
                 patch.object(module, '_RUNNING', set()), \
                 patch.object(db, claim, side_effect=RuntimeError('database locked')):
                kwargs = dict(account_id=2, email='expired@example.test', trigger='inspection')
                if module is plan_service:
                    kwargs['access_token'] = 'token'
                with self.assertRaises(RuntimeError):
                    getattr(module, enqueue)(**kwargs)
                self.assertNotIn(2, module._RUNNING)
                self.assertTrue(slots.acquire(blocking=False))
                slots.release()

    def test_slow_queued_checks_are_not_reenqueued_even_when_database_claim_expires(self):
        for module, claim, enqueue, prefix in (
            (service.live_check_service, 'claim_account_live_check', 'enqueue_account_live_check', 'live_check'),
            (plan_service, 'claim_account_plan_check', 'enqueue_account_plan_check', 'plan_check'),
        ):
            with self.subTest(module=module.__name__), \
                 patch.object(module, '_QUEUE_SLOTS', threading.BoundedSemaphore(2)), \
                 patch.object(module, '_RUNNING', set()):
                executor = Mock()
                kwargs = dict(account_id=2, email='expired@example.test', trigger='inspection', executor=executor)
                if module is plan_service:
                    kwargs['access_token'] = 'token'
                with patch.object(service.live_check_service, '_append_log'):
                    self.assertTrue(getattr(module, enqueue)(**kwargs)['accepted'])
                    rows = db._load_accounts()
                    row = next(r for r in rows if r['id'] == 2)
                    row[prefix + '_queued_at'] = '2000-01-01T00:00:00'
                    db._save_accounts(rows)
                    with patch.object(db, claim, wraps=getattr(db, claim)) as claimed:
                        self.assertTrue(getattr(module, enqueue)(**kwargs)['busy'])
                        claimed.assert_not_called()
                executor.submit.assert_called_once()

    def test_log_write_failure_does_not_leave_account_busy_or_leak_slot(self):
        module = service.live_check_service
        slots = threading.BoundedSemaphore(1)
        with patch.object(module, '_QUEUE_SLOTS', slots), \
             patch.object(module, '_RUNNING', set()), \
             patch.object(module, '_append_log', side_effect=OSError('disk full')), \
             self.assertLogs(module.logger, level='ERROR'):
            response = module.enqueue_account_live_check(account_id=2, email='expired@example.test')
            self.assertFalse(response['accepted'])
            self.assertNotIn(2, module._RUNNING)
            self.assertEqual(db.get_account(2)['live_check_status'], 'failed')
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

    def test_non_string_token_is_invalid_instead_of_crashing_worker(self):
        for token in (None, 123, True, [], {}, b'bad'):
            with self.subTest(token=token):
                self.assertTrue(service.token_needs_refresh(token))

    def test_malformed_request_must_not_start_default_inspection(self):
        app = create_app(auth_code='inspection-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        with patch.object(service._EXECUTOR, 'submit') as submit:
            for content, content_type in (('{', 'application/json'), ('null', 'application/json'),
                                           ('workers=8', 'text/plain')):
                response = client.post('/api/accounts/inspections', data=content,
                                       content_type=content_type, headers=headers)
                self.assertEqual(response.status_code, 400)
            submit.assert_not_called()
        self.assertEqual(db.list_account_inspections()['total'], 0)

    def test_interrupted_run_cannot_be_revived_by_stale_worker(self):
        stale, _ = db.create_account_inspection([2])
        db.recover_interrupted_account_inspections()
        stale['status'] = 'running'
        self.assertFalse(db.save_account_inspection(stale))
        self.assertEqual(db.list_account_inspections()['items'][0]['status'], 'interrupted')
        with patch.object(service, '_check_live') as live:
            service._run(stale)
            live.assert_not_called()
        history = db.list_account_inspections()
        self.assertIsNone(history['active'])
        self.assertEqual(history['items'][0]['status'], 'interrupted')

    def test_interrupt_during_live_stops_following_plan_and_accounts(self):
        run, _ = db.create_account_inspection([2, 3], workers=1)
        def interrupt(account, executor):
            db.recover_interrupted_account_inspections()
            return {'ok': True, 'plan_result': {'ok': True}}
        with patch.object(service, '_check_live', side_effect=interrupt) as live, \
             patch.object(plan_service, 'enqueue_account_plan_check') as plan:
            service._run(run)
        live.assert_called_once()
        plan.assert_not_called()
        result = db.list_account_inspections()['items'][0]
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['checked_count'], 1)
        self.assertIsNone(db.list_account_inspections()['active'])

    def test_old_terminal_save_does_not_conflict_with_new_active_run(self):
        old, _ = db.create_account_inspection([2])
        db.recover_interrupted_account_inspections()
        new, created = db.create_account_inspection([2])
        self.assertTrue(created)
        old['status'] = 'running'
        self.assertFalse(db.save_account_inspection(old))
        self.assertEqual(db.list_account_inspections()['active']['id'], new['id'])

    def test_deleted_account_id_is_not_reused_and_inspection_skips_missing_account(self):
        with patch.object(service._EXECUTOR, 'submit') as submit:
            task, _ = service.start_inspection(archived='only')
            run = submit.call_args.args[1]
            lease = submit.call_args.args[2]
        db.delete_accounts([5])
        new_id = db.insert_account(email='replacement@example.test', access_token='bad')
        self.assertGreater(new_id, 5)
        with patch.object(service, '_check_live') as live, patch.object(plan_service, 'enqueue_account_plan_check') as plan:
            service._run_with_lease(run, lease)
        live.assert_not_called()
        plan.assert_not_called()
        result = db.list_account_inspections()['items'][0]
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['checked_count'], 0)
        self.assertEqual(result['invalid_count'], 0)
        self.assertEqual(result['skipped_count'], 1)
        self.assertEqual(result['archived'], 'only')
        self.assertNotIn('live_check_status', db.get_account(new_id))

    def test_account_id_high_watermark_survives_delete_all_and_reinitialization(self):
        db.delete_accounts([1, 2, 3, 4, 5])
        with patch.object(db, '_SQLITE_READY', False), patch.object(db, '_SQLITE_READY_PATH', None):
            first = db.insert_account(email='first@example.test', access_token='bad')
        self.assertGreater(first, 5)
        db.delete_accounts([first])
        added, skipped = db.import_registered_email_accounts([
            {'email': 'second@example.test', 'password': 'pw', 'client_id': 'client', 'refresh_token': 'refresh'}
        ], source='outlook')
        self.assertEqual((added, skipped), (1, 0))
        self.assertGreater(db.get_account_by_email('second@example.test')['id'], first)

    def test_account_id_reservations_are_unique_under_concurrency(self):
        db.list_accounts()
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: db._next_account_id(), range(40)))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), list(range(6, 46)))

    def test_repeated_app_initialization_does_not_interrupt_running_inspection(self):
        create_app(auth_code='inspection-test')
        entered = threading.Event()
        release = threading.Event()
        original_submit = service._EXECUTOR.submit
        futures = []
        def submit(*args, **kwargs):
            future = original_submit(*args, **kwargs)
            futures.append(future)
            return future
        def live(account, executor):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test release timed out')
            return {'ok': False}
        with patch.object(service._EXECUTOR, 'submit', side_effect=submit), \
             patch.object(service, '_check_live', side_effect=live):
            try:
                task, created = service.start_inspection(workers=1)
                self.assertTrue(created)
                self.assertTrue(entered.wait(5))
                # app 的其他恢复例程也不能破坏巡检阶段的账号状态。
                db.claim_account_live_check(4)
                db.mark_account_live_check_running(4)
                create_app(auth_code='inspection-test')
                self.assertEqual(db.list_account_inspections()['active']['id'], task['id'])
                self.assertEqual(db.list_account_inspections()['active']['status'], 'running')
                self.assertEqual(db.get_account(4)['live_check_status'], 'running')
                duplicate, created = service.start_inspection(archived='only')
                self.assertFalse(created)
                self.assertEqual(duplicate['id'], task['id'])
                self.assertEqual(len(futures), 1)
            finally:
                release.set()
                for future in futures:
                    future.result(timeout=10)
        self.assertIsNone(db.list_account_inspections()['active'])
        with service.startup_recovery_guard() as can_recover:
            self.assertTrue(can_recover)

    def test_queued_inspection_is_preserved_until_owner_lock_is_released(self):
        create_app(auth_code='inspection-test')
        with patch.object(service._EXECUTOR, 'submit') as submit:
            task, _ = service.start_inspection()
            lease = submit.call_args.args[2]
        create_app(auth_code='inspection-test')
        self.assertEqual(db.list_account_inspections()['active']['id'], task['id'])
        self.assertEqual(db.list_account_inspections()['active']['status'], 'queued')
        # 模拟进程退出释放锁，随后新实例可以正常恢复遗留任务。
        lease.close()
        create_app(auth_code='inspection-test')
        self.assertIsNone(db.list_account_inspections()['active'])
        self.assertEqual(db.list_account_inspections()['items'][0]['status'], 'interrupted')

    def test_inspection_lease_blocks_other_processes_and_is_released_on_process_exit(self):
        lease = service.acquire_inspection_lease()
        self.assertIsNotNone(lease)
        code = """
import sys
from pathlib import Path
from core import db
from core.inspection_lease import acquire_inspection_lease
db._ensure_sqlite = lambda: None
db._active_sqlite_path = lambda: Path(sys.argv[1])
lease = acquire_inspection_lease()
print('acquired' if lease else 'busy', flush=True)
"""
        def probe():
            return subprocess.run([sys.executable, '-c', code, str(db._active_sqlite_path())],
                                  check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            self.assertEqual(probe(), 'busy')
        finally:
            lease.close()
        self.assertEqual(probe(), 'acquired')
        after_exit = service.acquire_inspection_lease()
        self.assertIsNotNone(after_exit)
        after_exit.close()

    def test_successful_live_check_is_not_failed_by_completion_log_error(self):
        run, _ = db.create_account_inspection([2], workers=1)
        def append_log(email, line, **kwargs):
            if '完成' in line:
                raise OSError('log disk is full')
        module = service.live_check_service
        with patch.object(module, 'check_account_liveness', return_value={'ok': True, 'access_token': 'fresh-token', 'plan_result': {'ok': True}}), \
             patch.object(module, '_append_log', side_effect=append_log), \
             patch.object(module, '_configured_live_check_proxies', return_value=[]), \
             patch.object(module, 'resolve_plan_check_route', return_value={}), \
             patch.object(module, 'open_plan_check_proxy', return_value=(None, None)), \
             patch.object(plan_service, 'enqueue_account_plan_check', return_value=True) as plan:
            service._run(run)
        self.assertEqual(run['live_success_count'], 1)
        self.assertEqual(run['live_failed_count'], 0)
        self.assertEqual(run['plan_success_count'], 1)
        self.assertEqual(db.get_account(2)['live_check_status'], 'live')
        self.assertEqual(db.get_account(2)['access_token'], 'fresh-token')
        plan.assert_not_called()

    def test_relay_cleanup_error_does_not_discard_successful_live_result(self):
        run, _ = db.create_account_inspection([2], workers=1)
        relay = Mock()
        relay.close.side_effect = OSError('relay close failed')
        module = service.live_check_service
        with patch.object(module, 'check_account_liveness', return_value={'ok': True, 'access_token': 'fresh-token', 'plan_result': {'ok': True}}), \
             patch.object(module, '_append_log'), \
             patch.object(module, '_configured_live_check_proxies', return_value=[]), \
             patch.object(module, 'resolve_plan_check_route', return_value={}), \
             patch.object(module, 'open_plan_check_proxy', return_value=(None, relay)), \
             patch.object(plan_service, 'enqueue_account_plan_check', return_value=True):
            service._run(run)
        self.assertEqual(run['live_success_count'], 1)
        self.assertEqual(run['plan_success_count'], 1)
        self.assertEqual(db.get_account(2)['access_token'], 'fresh-token')
        self.assertNotIn(2, module._RUNNING)

    def test_status_poll_recovers_orphan_after_worker_cannot_save_final_status(self):
        app = create_app(auth_code='inspection-test')
        with patch.object(service._EXECUTOR, 'submit') as submit:
            task, _ = service.start_inspection()
            run, lease = submit.call_args.args[1:]
        with patch.object(db, 'save_account_inspection', side_effect=OSError('database temporarily unavailable')), \
             self.assertLogs(service.logger, level='ERROR'):
            with self.assertRaises(OSError):
                service._run_with_lease(run, lease)
        self.assertEqual(db.list_account_inspections()['active']['id'], task['id'])
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        response = client.get('/api/accounts/inspections', headers=headers)
        self.assertEqual(response.status_code, 200)
        history = response.get_json()
        self.assertIsNone(history['active'])
        self.assertEqual(history['items'][0]['status'], 'interrupted')
        with patch.object(service._EXECUTOR, 'submit'):
            self.assertEqual(client.post('/api/accounts/inspections', headers=headers).status_code, 202)

    def test_malformed_jwt_structure_is_not_treated_as_valid(self):
        token = jwt({'exp': 9999999999})
        header, payload, signature = token.split('.')
        for malformed in (f'{header}.{payload}', f'{header}.{payload}.',
                          f'.{payload}.{signature}', token + '.extra'):
            with self.subTest(token=malformed):
                self.assertTrue(service.token_needs_refresh(malformed))
        self.assertFalse(service.token_needs_refresh('Bearer ' + token))

    def test_history_page_overflow_is_client_error_instead_of_server_error(self):
        app = create_app(auth_code='inspection-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        for page in (str(10 ** 50), '0', '-1', 'not-a-number'):
            with self.subTest(page=page):
                response = client.get('/api/accounts/inspections?page=' + page, headers=headers)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()['ok'])

    def test_cancelled_queued_stage_releases_account_and_queue_slot(self):
        for module, enqueue, status_key in (
            (service.live_check_service, 'enqueue_account_live_check', 'live_check_status'),
            (plan_service, 'enqueue_account_plan_check', 'plan_check_status'),
        ):
            release = threading.Event()
            slots = threading.BoundedSemaphore(1)
            with self.subTest(module=module.__name__), \
                 patch.object(module, '_QUEUE_SLOTS', slots), \
                 patch.object(module, '_RUNNING', set()), \
                 patch.object(service.live_check_service, '_append_log'), \
                 ThreadPoolExecutor(max_workers=1) as executor:
                blocker = executor.submit(release.wait, 10)
                try:
                    kwargs = dict(account_id=2, email='expired@example.test', trigger='inspection',
                                  return_future=True, executor=executor)
                    if module is plan_service:
                        kwargs['access_token'] = 'token'
                    queued = getattr(module, enqueue)(**kwargs)
                    self.assertTrue(queued['accepted'])
                    self.assertTrue(queued['future'].cancel())
                    self.assertNotIn(2, module._RUNNING)
                    self.assertEqual(db.get_account(2)[status_key], 'failed')
                    self.assertTrue(slots.acquire(blocking=False))
                    slots.release()
                    again = getattr(module, enqueue)(**kwargs)
                    self.assertTrue(again['accepted'])
                    self.assertTrue(again['future'].cancel())
                finally:
                    release.set()
                    blocker.result(timeout=10)

    def test_cancelled_queued_inspection_releases_process_lease(self):
        release = threading.Event()
        original_submit = service._EXECUTOR.submit
        blocker = original_submit(release.wait, 10)
        futures = []
        def submit(*args, **kwargs):
            future = original_submit(*args, **kwargs)
            futures.append(future)
            return future
        with patch.object(service._EXECUTOR, 'submit', side_effect=submit):
            try:
                task, created = service.start_inspection()
                self.assertTrue(created)
                self.assertTrue(futures[0].cancel())
                history = db.list_account_inspections()
                self.assertIsNone(history['active'])
                self.assertEqual(history['items'][0]['status'], 'interrupted')
                with service.startup_recovery_guard() as can_recover:
                    self.assertTrue(can_recover)
                _, created = service.start_inspection()
                self.assertTrue(created)
                self.assertTrue(futures[1].cancel())
            finally:
                release.set()
                for future in futures:
                    future.cancel()
                blocker.result(timeout=10)

    def test_live_result_cannot_restore_token_after_email_change(self):
        run, _ = db.create_account_inspection([2], workers=1)
        def check(*args, **kwargs):
            db.finish_account_email_change(2, ok=True, new_email='new@example.test')
            return {'ok': True, 'access_token': 'obsolete-token'}
        module = service.live_check_service
        with patch.object(module, 'check_account_liveness', side_effect=check), \
             patch.object(module, '_append_log'), \
             patch.object(module, '_configured_live_check_proxies', return_value=[]), \
             patch.object(module, 'resolve_plan_check_route', return_value={}), \
             patch.object(module, 'open_plan_check_proxy', return_value=(None, None)), \
             patch.object(plan_service, 'enqueue_account_plan_check') as plan:
            service._run(run)
        account = db.get_account(2)
        self.assertEqual(account['email'], 'new@example.test')
        self.assertEqual(account['access_token'], '')
        self.assertEqual(account['live_check_status'], '')
        self.assertEqual(run['live_success_count'], 0)
        self.assertEqual(run['live_failed_count'], 1)
        plan.assert_not_called()
        self.assertNotIn(2, module._RUNNING)

    def test_plan_result_cannot_overwrite_entitlements_after_token_refresh(self):
        module = plan_service
        old = db.get_account(2)
        def check(*args, **kwargs):
            db.update_account_liveness(2, {'ok': True, 'access_token': 'new-token'})
            db.update_account_plan_check(acc_id=2, result={'ok': True, 'current_plan_type': 'plus'})
            return {'ok': True, 'current_plan_type': 'free'}
        with patch.object(module, 'check_account_plan', side_effect=check), \
             patch.object(module, '_wait_for_rate_slot'), \
             ThreadPoolExecutor(max_workers=1) as executor:
            queued = module.enqueue_account_plan_check(account_id=2, email=old['email'],
                access_token=old['access_token'], trigger='inspection', return_future=True, executor=executor)
            result = queued['future'].result(timeout=5)
        self.assertFalse(result['ok'])
        self.assertEqual(db.get_account(2)['current_plan_type'], 'plus')
        self.assertEqual(db.get_account(2)['access_token'], 'new-token')
        self.assertNotIn(2, module._RUNNING)

    def test_deleted_account_during_live_is_not_reported_as_success(self):
        run, _ = db.create_account_inspection([2], workers=1)
        def check(*args, **kwargs):
            db.delete_accounts([2])
            return {'ok': True, 'access_token': 'token'}
        module = service.live_check_service
        with patch.object(module, 'check_account_liveness', side_effect=check), \
             patch.object(module, '_append_log'), \
             patch.object(module, '_configured_live_check_proxies', return_value=[]), \
             patch.object(module, 'resolve_plan_check_route', return_value={}), \
             patch.object(module, 'open_plan_check_proxy', return_value=(None, None)), \
             patch.object(plan_service, 'enqueue_account_plan_check') as plan:
            service._run(run)
        self.assertEqual(run['live_success_count'], 0)
        self.assertEqual(run['live_failed_count'], 1)
        self.assertEqual(run['status'], 'completed')
        plan.assert_not_called()

    def test_snapshot_rejects_failed_results_without_modifying_changed_account(self):
        old = db.get_account(2)
        db.finish_account_email_change(2, ok=True, new_email='changed@example.test')
        before = db.get_account(2)
        self.assertFalse(db.update_account_liveness(2, {'ok': False, 'error': 'old failure'},
                         expected_account=old))
        self.assertFalse(db.update_account_plan_check(acc_id=2, result={'ok': False, 'error': 'old failure'},
                         expected_account=old))
        self.assertEqual(db.get_account(2), before)

    def test_manual_live_saves_token_before_same_session_plan_and_never_resaves_old_live(self):
        module = service.live_check_service
        owner = object()
        def auth(email, **kwargs):
            return kwargs['on_authenticated']({'ok': True, 'access_token': 'fresh-token'}, owner)
        def query(token, **kwargs):
            self.assertIs(kwargs['session'], owner)
            self.assertEqual(token, 'fresh-token')
            # 登录 token 在发起套餐请求前已写回；后续变化不会被旧查活结果覆盖。
            self.assertEqual(db.get_account(2)['access_token'], 'fresh-token')
            self.assertTrue(module.is_checking('expired@example.test'))
            db.update_account_liveness(2, {'ok': True, 'access_token': 'even-newer-token'})
            return {'ok': True, 'current_plan_type': 'free'}
        with patch.object(module, 'check_account_liveness', side_effect=auth), \
             patch.object(module, '_append_log'), \
             patch.object(module, '_configured_live_check_proxies', return_value=[]), \
             patch.object(module, 'resolve_plan_check_route', return_value={}), \
             patch.object(module, 'open_plan_check_proxy', return_value=(None, None)), \
             patch.object(plan_service, '_wait_for_rate_slot'), \
             patch.object(plan_service, 'check_account_plan', side_effect=query) as plan:
            queued = module.enqueue_account_live_check(account_id=2, email='expired@example.test', return_future=True)
            result = queued['future'].result(timeout=5)
        self.assertTrue(result['ok'])
        self.assertFalse(result['plan_result']['ok'])
        self.assertEqual(db.get_account(2)['access_token'], 'even-newer-token')
        self.assertFalse(module.is_checking('expired@example.test'))
        self.assertEqual(db.get_account(2)['plan_check_status'], 'failed')
        plan.assert_called_once()

    def test_live_auto_plan_failure_keeps_live_token_and_previous_entitlements(self):
        account = db.get_account(2)
        db.update_account_plan_check(acc_id=2, result={'ok': True, 'current_plan_type': 'plus'})
        with patch.object(plan_service, '_wait_for_rate_slot'), \
             patch.object(plan_service, 'check_account_plan', side_effect=RuntimeError('network failure')), \
             patch.object(service.live_check_service, '_append_log'):
            result = service.live_check_service.complete_authenticated_live_check(
                account_id=2, expected_account=account, result={'ok': True, 'access_token': 'fresh-token'}, session=object())
        self.assertTrue(result['ok'])
        self.assertFalse(result['plan_result']['ok'])
        saved = db.get_account(2)
        self.assertEqual(saved['access_token'], 'fresh-token')
        self.assertEqual(saved['live_check_status'], 'live')
        self.assertEqual(saved['plan_check_status'], 'failed')
        self.assertEqual(saved['current_plan_type'], 'plus')
        self.assertNotIn(2, plan_service._RUNNING)

    def test_auto_plan_obeys_shared_busy_guard_without_duplicate_network_request(self):
        account = db.get_account(2)
        with patch.object(plan_service, '_RUNNING', {2}), \
             patch.object(plan_service, 'check_account_plan') as plan, \
             patch.object(service.live_check_service, '_append_log'):
            result = service.live_check_service.complete_authenticated_live_check(
                account_id=2, expected_account=account, result={'ok': True, 'access_token': 'fresh-token'}, session=object())
        self.assertTrue(result['ok'])
        self.assertFalse(result['plan_result']['ok'])
        self.assertTrue(result['plan_result']['busy'])
        plan.assert_not_called()

    def test_changed_account_rejects_live_before_plan_request(self):
        old = db.get_account(2)
        db.finish_account_email_change(2, ok=True, new_email='changed@example.test')
        with patch.object(plan_service, 'check_account_plan_in_session') as plan:
            result = service.live_check_service.complete_authenticated_live_check(
                account_id=2, expected_account=old, result={'ok': True, 'access_token': 'stale-token'}, session=object())
        self.assertFalse(result['ok'])
        plan.assert_not_called()
        self.assertEqual(db.get_account(2)['access_token'], '')

    def test_old_plan_run_cannot_finish_newer_claim(self):
        self.assertTrue(db.claim_account_plan_check(2, run_id='old-run'))
        self.assertTrue(db.mark_account_plan_check_running(2, run_id='old-run'))
        old = db.get_account(2)
        # 模拟另一执行者恢复后成功领取了同账号的新查询。
        db.update_account_plan_check(acc_id=2, result={'ok': False, 'error': 'interrupted'})
        self.assertTrue(db.claim_account_plan_check(2, run_id='new-run'))
        queued = db.get_account(2)
        self.assertFalse(db.mark_account_plan_check_running(2, run_id='old-run'))
        self.assertFalse(db.update_account_plan_check(acc_id=2, result={'ok': False, 'error': 'late cancellation'},
                         expected_run_id='old-run'))
        self.assertEqual(db.get_account(2), queued)
        self.assertTrue(db.mark_account_plan_check_running(2, run_id='new-run'))
        before = db.get_account(2)
        self.assertFalse(db.update_account_plan_check(acc_id=2,
            result={'ok': True, 'current_plan_type': 'free'},
            expected_account=old, expected_run_id='old-run'))
        self.assertEqual(db.get_account(2), before)

    def test_auth_callback_save_failure_then_retry_does_not_leave_live_running(self):
        module = service.live_check_service
        attempts = []
        def auth(email, **kwargs):
            attempts.append(email)
            if len(attempts) == 1:
                # 与真实底层查活相同：回调异常转成失败结果，由上层选择另一个出口。
                try:
                    return kwargs['on_authenticated']({'ok': True, 'access_token': 'fresh-token'}, object())
                except OSError as exc:
                    return {'ok': False, 'status': 'failed', 'error': str(exc)}
            return {'ok': False, 'status': 'failed', 'error': 'login rejected'}
        slots = threading.BoundedSemaphore(1)
        with patch.object(module, '_QUEUE_SLOTS', slots), \
             patch.object(module, 'check_account_liveness', side_effect=auth), \
             patch.object(module, 'complete_authenticated_live_check', side_effect=OSError('database timeout')), \
             patch.object(module, '_append_log'), \
             patch.object(module, '_configured_live_check_proxies', return_value=['proxy-a', 'proxy-b']), \
             patch.object(module, 'resolve_plan_check_route', return_value={'proxy': 'proxy-a'}), \
             patch.object(module, '_live_proxy_route', return_value={'proxy': 'proxy-b'}), \
             patch.object(module, 'open_plan_check_proxy', return_value=(None, None)):
            queued = module.enqueue_account_live_check(account_id=2, email='expired@example.test', return_future=True)
            result = queued['future'].result(timeout=5)
        self.assertFalse(result['ok'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(db.get_account(2)['live_check_status'], 'failed')
        self.assertEqual(db.get_account(2)['live_check_error'], 'login rejected')
        self.assertNotIn(2, module._RUNNING)
        self.assertTrue(slots.acquire(blocking=False))

    def test_plan_start_database_error_releases_persisted_queue_claim(self):
        with patch.object(plan_service.db, 'mark_account_plan_check_running', side_effect=OSError('database temporarily locked')), \
             patch.object(plan_service, 'check_account_plan') as network:
            result = plan_service.check_account_plan_in_session(account_id=2, email='expired@example.test',
                access_token=db.get_account(2)['access_token'], session=object())
        self.assertFalse(result['ok'])
        self.assertEqual(db.get_account(2)['plan_check_status'], 'failed')
        self.assertNotIn(2, plan_service._RUNNING)
        network.assert_not_called()

    def test_live_ownership_rejects_old_start_result_and_cancellation(self):
        self.assertTrue(db.claim_account_live_check(2, run_id='live-old', expected_email='expired@example.test'))
        self.assertTrue(db.mark_account_live_check_running(2, run_id='live-old'))
        db.recover_interrupted_live_checks()
        self.assertTrue(db.claim_account_live_check(2, run_id='live-new', expected_email='expired@example.test'))
        before = db.get_account(2)
        self.assertFalse(db.mark_account_live_check_running(2, run_id='live-old'))
        for result in ({'ok': False, 'error': 'late cancellation'}, {'ok': True, 'access_token': 'stale'}):
            self.assertFalse(db.update_account_liveness(2, result, expected_run_id='live-old'))
        self.assertEqual(db.get_account(2), before)

    def test_stale_plan_waiting_for_rate_slot_does_not_send_old_token(self):
        old = db.get_account(2)
        def refresh():
            db.update_account_liveness(2, {'ok': True, 'access_token': 'new-token'})
        with patch.object(plan_service, '_wait_for_rate_slot', side_effect=refresh), \
             patch.object(plan_service, 'check_account_plan') as request:
            result = plan_service.check_account_plan_in_session(account_id=2, email=old['email'],
                access_token=old['access_token'], session=object())
        self.assertFalse(result['ok'])
        request.assert_not_called()
        self.assertEqual(db.get_account(2)['plan_check_status'], 'failed')
        self.assertEqual(db.get_account(2)['access_token'], 'new-token')

    def test_fresh_live_token_clears_old_expiry_metadata_even_when_plan_busy(self):
        db.update_account_plan_check(acc_id=2, result={'ok': False, 'token_expired': True,
                                                     'token_expires_at': '1970-01-01T00:00:01Z'})
        old = db.get_account(2)
        fresh = jwt({'exp': 2000000000})
        with patch.object(plan_service, '_RUNNING', {2}), \
             patch.object(service.live_check_service, '_append_log'):
            result = service.live_check_service.complete_authenticated_live_check(account_id=2,
                expected_account=old, result={'ok': True, 'access_token': fresh}, session=object())
        self.assertTrue(result['ok'])
        self.assertTrue(result['plan_result']['busy'])
        saved = db.get_account(2)
        self.assertFalse(saved['token_expired'])
        self.assertNotEqual(saved['token_expires_at'], old['token_expires_at'])

    def test_real_cancelled_old_live_future_does_not_fail_new_database_claim(self):
        module = service.live_check_service
        future = Future()
        with patch.object(module._EXECUTOR, 'submit', return_value=future), \
             patch.object(module, '_append_log'):
            queued = module.enqueue_account_live_check(account_id=2, email='expired@example.test', return_future=True)
            self.assertTrue(queued['accepted'])
            db.recover_interrupted_live_checks()
            # 模拟别的执行者领取新任务，本进程仍持有旧 Future。
            self.assertTrue(db.claim_account_live_check(2))
            before = db.get_account(2)
            self.assertTrue(future.cancel())
        self.assertEqual(db.get_account(2), before)
        self.assertNotIn(2, module._RUNNING)

    def test_plan_transport_error_preserves_current_token_metadata(self):
        db.update_account_liveness(2, {'ok': True, 'access_token': jwt({'exp': 2000000000})})
        before = db.get_account(2)
        db.update_account_plan_check(acc_id=2, result={'ok': False, 'error': 'network unavailable'})
        after = db.get_account(2)
        self.assertIs(after['token_expired'], False)
        self.assertEqual(after['token_expires_at'], before['token_expires_at'])

    def test_mixed_manual_and_inspection_stress_has_no_duplicate_accounts_or_stat_drift(self):
        module = service.live_check_service
        ids = [2, 3] + [db.insert_account(email=f'stress-{i}@example.test', access_token=jwt({'exp': 1}))
                       for i in range(14)]
        original_enqueue = module.enqueue_account_live_check
        for workers in (1, 4, 16, 32):
            with self.subTest(workers=workers):
                for account_id in ids:
                    db.update_account_liveness(account_id, {'ok': True, 'access_token': jwt({'exp': 1})})
                emails = {db.get_account(i)['email']: i for i in ids}
                manual_ids = set(ids[:2])
                release = threading.Event()
                entered = threading.Condition()
                busy_seen = threading.Event()
                active_ids, collisions, calls, owners, attempted = set(), [], [], {}, set()
                manual_started = []
                mismatches = []
                def auth(email, **kwargs):
                    account_id = emails[email]
                    with entered:
                        if account_id in active_ids:
                            collisions.append(account_id)
                        active_ids.add(account_id)
                        calls.append(account_id)
                        if account_id in manual_ids:
                            manual_started.append(account_id)
                            entered.notify_all()
                    try:
                        if account_id in manual_ids and not release.wait(10):
                            raise TimeoutError('manual workers did not release')
                        if account_id % 5 == 0:
                            return {'ok': False, 'status': 'failed', 'error': 'login rejected'}
                        owner = object()
                        with entered:
                            owners[owner] = account_id
                        fresh = jwt({'exp': 9999999999, 'account': account_id})
                        return kwargs['on_authenticated']({'ok': True, 'access_token': fresh}, owner)
                    finally:
                        with entered:
                            active_ids.discard(account_id)
                def plan(token, **kwargs):
                    account_id = owners[kwargs['session']]
                    if db.get_account(account_id)['access_token'] != token:
                        mismatches.append(account_id)
                    return {'ok': account_id % 3 != 0, 'current_plan_type': 'plus', 'error': 'test refusal'}
                def enqueue(**kwargs):
                    result = original_enqueue(**kwargs)
                    if kwargs.get('trigger') == 'inspection' and kwargs['account_id'] in manual_ids:
                        self.assertFalse(result['accepted'])
                        with entered:
                            attempted.add(kwargs['account_id'])
                            if attempted == manual_ids:
                                busy_seen.set()
                    return result
                with patch.object(module, 'check_account_liveness', side_effect=auth), \
                     patch.object(module, 'enqueue_account_live_check', side_effect=enqueue), \
                     patch.object(module, '_append_log'), \
                     patch.object(module, '_configured_live_check_proxies', return_value=[]), \
                     patch.object(module, 'resolve_plan_check_route', return_value={}), \
                     patch.object(module, 'open_plan_check_proxy', return_value=(None, None)), \
                     patch.object(plan_service, '_wait_for_rate_slot'), \
                     patch.object(plan_service, 'check_account_plan', side_effect=plan), \
                     ThreadPoolExecutor(max_workers=1) as runner:
                    manual = []
                    inspection = None
                    try:
                        manual = [original_enqueue(account_id=i, email=db.get_account(i)['email'],
                                                   return_future=True)['future'] for i in ids[:2]]
                        with entered:
                            self.assertTrue(entered.wait_for(lambda: len(manual_started) == 2, timeout=5))
                        run, _ = db.create_account_inspection(ids + ids, workers=workers)
                        inspection = runner.submit(service._run, run)
                        self.assertTrue(busy_seen.wait(5))
                    finally:
                        release.set()
                        for future in manual:
                            future.result(timeout=10)
                        if inspection is not None:
                            inspection.result(timeout=15)
                self.assertEqual(collisions, [])
                self.assertEqual(mismatches, [])
                self.assertEqual(sorted(calls), sorted(ids))
                self.assertEqual(active_ids, set())
                self.assertEqual(run['status'], 'completed')
                self.assertEqual(run['total_count'], len(ids))
                self.assertEqual(run['checked_count'] + run['skipped_count'], len(ids))
                self.assertEqual(run['live_success_count'] + run['live_failed_count'], run['invalid_count'])
                self.assertEqual(run['plan_success_count'] + run['plan_failed_count'], run['live_success_count'])
                success_ids = [i for i in ids if i not in manual_ids and i % 5 != 0]
                self.assertEqual(run['live_success_count'], len(success_ids))
                self.assertEqual(run['live_failed_count'], len(ids) - len(success_ids))
                self.assertEqual(run['plan_success_count'], sum(i % 3 != 0 for i in success_ids))
                self.assertEqual(run['plan_failed_count'], sum(i % 3 == 0 for i in success_ids))
                self.assertIsNone(db.list_account_inspections()['active'])
                self.assertEqual(db.list_account_inspection_failures(run['id'])['total'], run['live_failed_count'])
                for account_id in ids:
                    self.assertNotIn(account_id, module._RUNNING)
                    self.assertNotIn(account_id, plan_service._RUNNING)
                    self.assertNotIn(db.get_account(account_id).get('live_check_status'), ('queued', 'running'))
                    self.assertNotIn(db.get_account(account_id).get('plan_check_status'), ('queued', 'running'))

    def test_submission_failure_releases_active_task(self):
        with patch.object(service._EXECUTOR, 'submit', side_effect=RuntimeError('executor unavailable')):
            with self.assertRaises(RuntimeError):
                service.start_inspection()
        history = db.list_account_inspections()
        self.assertIsNone(history['active'])
        self.assertEqual(history['items'][0]['status'], 'failed')

    def test_api_and_both_ui_templates(self):
        app = create_app(auth_code='inspection-test')
        client = app.test_client()
        headers = {'X-Auth-Code': 'inspection-test'}
        self.assertEqual(client.post('/api/accounts/inspections').status_code, 401)
        with patch.object(service._EXECUTOR, 'submit'):
            response = client.post('/api/accounts/inspections', headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(client.post('/api/accounts/inspections', headers=headers).status_code, 409)
        history = client.get('/api/accounts/inspections', headers=headers).get_json()
        self.assertEqual(history['total'], 1)
        self.assertEqual(history['active']['total_count'], 4)
        self.assertNotIn('account_ids', history['active'])
        self.assertEqual(client.get('/api/accounts/inspections?page=bad', headers=headers).status_code, 400)
        for ui in ('v2', 'legacy'):
            response = client.get('/?ui=' + ui, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn('inspectionHistoryDialog', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
