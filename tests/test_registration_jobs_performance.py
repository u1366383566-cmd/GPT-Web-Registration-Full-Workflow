import json
import gzip
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db, registration_service as svc


class RegistrationJobsPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        missing = root / 'missing.json'
        self.stack.enter_context(patch.multiple(
            db, _ACCOUNTS_JSON=root/'accounts.json', _JOBS_JSON=root/'jobs.json',
            _OUTLOOK_JSON=missing, _GENERIC_API_EMAIL_JSON=missing, _DOMAIN_EMAIL_JSON=missing,
            _LEGACY_ACCOUNTS_JSON=missing, _LEGACY_JOBS_JSON=missing, _LEGACY_OUTLOOK_JSON=missing,
            _LEGACY_SQLITE=root/'missing.db', _CODEX_DIR=root/'codex', _CODEX_AGENT_DIR=root/'agent',
            _LEGACY_CODEX_EXPORT_STATE=missing, _LOG_DIR=root/'logs',
            _SQLITE_READY=False, _SQLITE_READY_PATH=None))
        db._ensure_sqlite()

    def seed(self, count):
        rows = [dict(id=i+1, email=f'user{i}@example.com', status='failed',
                     email_source='outlook', created_at='2026-01-01', error_message='old')
                for i in range(count)]
        db._save_jobs(rows)
        return rows

    def test_batch_create_update_get_do_not_load_or_rewrite_collection(self):
        self.seed(1000)
        with patch.object(db, '_load_jobs', side_effect=AssertionError('全表读取')), \
             patch.object(db, '_save_jobs', side_effect=AssertionError('全表重写')):
            jobs = db.create_jobs(200, 'remail')
            self.assertEqual([j['id'] for j in jobs], list(range(1001, 1201)))
            self.assertEqual(len({j['log_file'] for j in jobs}), 200)
            before = db.get_job(1)
            db.update_job(1001, status='running', email='new@example.com', error='new',
                          network_traffic={'available': True}, account_id=12)
            self.assertEqual(db.get_job(1), before)
            changed = db.get_job(1001)
            self.assertEqual(changed['email'], 'new@example.com')
            self.assertEqual(changed['account_id'], 12)
            self.assertEqual(changed['status'], 'running')
            self.assertEqual(changed['network_traffic'], {'available': True})
            self.assertEqual([r['id'] for r in db.get_jobs_by_ids([1200,1001])], [1200,1001])
            self.assertIsNone(db.get_job(99999))
            db.update_job(99999, status='success')

    def test_concurrent_submission_allocates_distinct_ids(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            batches = list(pool.map(lambda _: db.create_jobs(20, 'outlook'), range(5)))
        ids = [r['id'] for batch in batches for r in batch]
        self.assertEqual(sorted(ids), list(range(1,101)))

    def test_pagination_counts_and_revision(self):
        db.create_jobs(15, 'outlook')
        db.update_job(3, status='running')
        db.update_job(4, status='stopping')
        db.update_job(5, status='success')
        result = db.list_jobs_page(limit=5, offset=5)
        self.assertEqual([r['id'] for r in result['items']], [10,9,8,7,6])
        self.assertEqual(result['total'], 15)
        self.assertEqual(result['status_counts'], {'pending':12,'running':1,'stopping':1,'success':1,'active':14})
        db.update_job(1, error='modified')
        self.assertNotEqual(db.list_jobs_page()['revision'], result['revision'])
        empty = db.list_jobs_page(offset=100)
        self.assertEqual(empty['items'], [])
        self.assertEqual(empty['total'], 15)

    def test_indexed_query_plans(self):
        self.seed(200)
        with closing(db._sqlite_conn()) as conn:
            plans = [
                ("SELECT payload FROM registration_jobs ORDER BY id DESC LIMIT 50 OFFSET 50", 'idx_registration_jobs_id'),
                ("SELECT payload FROM registration_jobs WHERE id=10", 'idx_registration_jobs_id'),
                ("SELECT MAX(updated_at) FROM registration_jobs", 'idx_registration_jobs_updated'),
                ("SELECT id FROM registration_jobs WHERE CAST(json_extract(payload, '$.root_job_id') AS INTEGER)=1 AND status='success'", 'idx_registration_jobs_root_status'),
            ]
            for sql, index in plans:
                detail = ' '.join(str(tuple(r)) for r in conn.execute('EXPLAIN QUERY PLAN '+sql))
                self.assertIn(index, detail)
                self.assertNotIn('USE TEMP B-TREE', detail)

    def test_retry_context_matches_individual_lookup(self):
        jobs = self.seed(12)
        jobs[1]['account_id'] = 1
        jobs[2]['account_id'] = 404
        jobs[2]['email'] = 'MATCH@Example.com'
        jobs[3]['account_id'] = 2
        jobs[4]['account_id'] = 3
        jobs[5]['status'] = 'cancelled'
        jobs[6].update(root_job_id=1, status='success')
        jobs[7].update(root_job_id=1, status='success')
        jobs[8].update(status='success')
        jobs[9].update(status='pending')
        jobs[10].update(root_job_id=1)
        db._save_jobs(jobs)
        db._save_accounts([
            {'id':1,'email':'match@example.com','codex_status':'success'},
            {'id':2,'email':'retry@example.com','codex_status':'failed'},
            {'id':3,'email':'disabled@example.com','codex_status':'deactivated'},
        ])
        # 独立构造旧实现结果，避免仅比较两个共用新查询的路径。
        expected = {}
        for job in jobs:
            root = int(job.get('root_job_id') or job['id'])
            matches = [r for r in jobs if r['id'] != job['id'] and r.get('root_job_id') == root and r['status']=='success']
            success = max(matches, key=lambda r:r['id']) if matches else None
            with patch.object(db, 'get_successful_retry_for_job', return_value=success):
                expected[job['id']] = svc.get_retry_info(job)
        with patch.object(db, '_load_jobs', side_effect=AssertionError('全表读取')), \
             patch.object(db, 'get_successful_retry_for_job', side_effect=AssertionError('逐行重试查询')), \
             patch.object(svc, '_account_for_job', side_effect=AssertionError('逐行账号查询')):
            enriched = [dict(job) for job in jobs]
            svc.enrich_jobs_retry_info(enriched)
        for row in enriched:
            for key, value in expected[row['id']].items():
                self.assertEqual(row.get(key), value, (row['id'],key))

    def test_submit_uses_batch_and_preserves_submission_failure(self):
        executor = MagicMock()
        executor.submit.side_effect = [object(), RuntimeError('queue closed'), object()]
        with patch.object(svc, 'get_executor', return_value=executor), \
             patch.object(svc, 'get_executor_workers', return_value=2), \
             patch.object(db, 'create_jobs', wraps=db.create_jobs) as create, \
             patch.object(db, 'create_job', side_effect=AssertionError('逐条创建')), \
             patch.object(db, 'get_job', side_effect=AssertionError('逐条读取')):
            jobs = svc.submit_registration(count=3, email_source='outlook', workers=2)
        create.assert_called_once_with(count=3, email_source='outlook')
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[1]['status'], 'failed')
        self.assertIn('queue closed', jobs[1]['error_message'])
        self.assertEqual(executor.submit.call_count, 3)

    def test_jobs_api_uses_batched_context(self):
        from webui.app import create_app
        # 禁止测试初始化回收正式/临时任务。
        with patch.object(db, 'recover_interrupted_registration_jobs', return_value=0):
            client = create_app(auth_code='test-auth').test_client()
        client.environ_base['HTTP_X_AUTH_CODE'] = 'test-auth'
        self.seed(80)
        with patch.object(db, '_load_jobs', side_effect=AssertionError('全表读取')), \
             patch.object(db, 'job_status_counts', side_effect=AssertionError('重复聚合')):
            response = client.get('/api/jobs?paged=1&page=2&page_size=10')
            self.assertEqual(response.status_code, 200)
            payload = json.loads(gzip.decompress(response.data)) if response.headers.get('Content-Encoding') == 'gzip' else response.get_json()
            self.assertEqual(payload['total'], 80)
            self.assertEqual(payload['status_counts']['failed'], 80)
            self.assertEqual([r['id'] for r in payload['items']], list(range(70,60,-1)))
            self.assertTrue(payload['items'][0]['retryable'])
            self.assertTrue(payload['compact'])
            response = client.get('/api/jobs?limit=5')
            payload = json.loads(gzip.decompress(response.data)) if response.headers.get('Content-Encoding') == 'gzip' else response.get_json()
            self.assertEqual(len(payload),5)

    def test_retry_submission_is_indexed_and_reuses_active_child(self):
        self.seed(1000)
        with patch.object(db, '_load_jobs', side_effect=AssertionError('全表读取')), \
             patch.object(db, '_save_jobs', side_effect=AssertionError('全表重写')):
            child, created = db.create_retry_job(1, job_type='registration', email_source='outlook')
            self.assertTrue(created)
            self.assertEqual(child['parent_job_id'], 1)
            self.assertEqual(child['root_job_id'], 1)
            self.assertEqual(child['retry_attempt'], 1)
            same, created = db.create_retry_job(1, job_type='registration', email_source='outlook')
            self.assertFalse(created)
            self.assertEqual(same['id'], child['id'])
            with self.assertRaises(ValueError):
                db.create_retry_job(1, job_type='codex_retry', email_source='outlook')
            db.update_job(child['id'], status='failed')
            second, created = db.create_retry_job(child['id'], job_type='registration', email_source='outlook')
            self.assertTrue(created)
            self.assertEqual(second['retry_attempt'], 2)
            self.assertEqual(second['root_job_id'], 1)
            db.update_job(second['id'], status='success')
            self.assertEqual(db.get_successful_retry_for_job(1)['id'], second['id'])
            self.assertIsNone(db.get_successful_retry_for_job(second['id']))

    def test_retry_context_query_count_does_not_scale_per_row(self):
        jobs = self.seed(1000)
        original = db._sqlite_conn
        statements = []
        def traced_connection():
            connection = original()
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(db, '_sqlite_conn', side_effect=traced_connection):
            page = db.list_jobs_page(50)
            svc.enrich_jobs_retry_info(page['items'])
        selects = [q for q in statements if q.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(selects), 5)
        self.assertEqual(len(page['items']), 50)

    def test_batch_insert_rolls_back_on_failure(self):
        self.seed(5)
        original = db._insert_job_rows
        def failing_insert(conn, rows):
            original(conn, rows[:1])
            raise RuntimeError('insert failed')
        with patch.object(db, '_insert_job_rows', side_effect=failing_insert):
            with self.assertRaises(RuntimeError):
                db.create_jobs(3, 'outlook')
        self.assertEqual(db.list_jobs_page()['total'], 5)
        self.assertIsNone(db.get_job(6))

    def test_job_ids_never_reused_after_delete_restart_or_legacy_clear(self):
        first = db.create_job('outlook')
        self.assertTrue(db.delete_job(first['id']))
        db._SQLITE_READY = False
        second = db.create_job('outlook')
        self.assertGreater(second['id'], first['id'])
        db._save_jobs([])
        third = db.create_job('outlook')
        self.assertGreater(third['id'], second['id'])
        self.assertFalse(db.update_job(first['id'], status='running', expected_statuses=('pending',)))
        self.assertEqual(db.get_job(third['id'])['status'], 'pending')

    def test_bulk_cancel_and_recovery_do_not_load_history(self):
        db.create_jobs(1505, 'outlook')
        db.update_job(1, status='running')
        db.update_job(2, status='stopping')
        db.update_job(3, status='success')
        with patch.object(db, '_load_jobs', side_effect=AssertionError('全表读取')), \
             patch.object(db, '_save_jobs', side_effect=AssertionError('全表重写')):
            self.assertEqual(svc.cancel_pending_jobs(), 1502)
            self.assertEqual(db.get_job(1)['status'], 'running')
            self.assertEqual(db.recover_interrupted_registration_jobs(), 2)
            self.assertEqual(db.recover_interrupted_registration_jobs(), 0)
            self.assertEqual(db.get_job(1)['status'], 'stopped')
            self.assertIn('服务重启', db.get_job(1)['error_message'])
            self.assertEqual(db.get_job(3)['status'], 'success')
            self.assertTrue(db.delete_job(1505))

    def test_conditional_updates_and_noop_revision(self):
        job = db.create_job('outlook')
        self.assertTrue(db.update_job(job['id'], status='running', expected_statuses=('pending',)))
        self.assertFalse(db.update_job(job['id'], status='cancelled', expected_statuses=('pending',)))
        db.update_job(job['id'], status='success', email='a@example.com', network_traffic={'bytes': 5})
        revision = db.list_jobs_page()['revision']
        self.assertTrue(db.update_job(job['id'], email='a@example.com', network_traffic={'bytes': 5}))
        self.assertEqual(db.list_jobs_page()['revision'], revision)
        self.assertFalse(db.update_job(job['id'], status='stopping', expected_statuses=('running', 'stopping')))
        self.assertEqual(db.get_job(job['id'])['status'], 'success')

    def test_concurrent_claim_has_exactly_one_winner(self):
        job = db.create_job('outlook')
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: db.update_job(
                job['id'], status='running', expected_statuses=('pending',)), range(30)))
        self.assertEqual(sum(outcomes), 1)

    def test_concurrent_metadata_updates_do_not_lose_fields(self):
        job = db.create_job('outlook')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(db.update_job, job['id'], email='a@example.com'),
                       pool.submit(db.update_job, job['id'], network_traffic={'bytes': 9})]
            self.assertTrue(all(f.result() for f in futures))
        row = db.get_job(job['id'])
        self.assertEqual(row['email'], 'a@example.com')
        self.assertEqual(row['network_traffic'], {'bytes': 9})

    def test_cancel_races_with_claim(self):
        import threading
        for _ in range(15):
            job = db.create_job('outlook')
            barrier = threading.Barrier(2)
            def claim():
                barrier.wait()
                return db.update_job(job['id'], status='running', expected_statuses=('pending',))
            def cancel():
                barrier.wait()
                return db.cancel_pending_registration_jobs()
            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed, cancelled = pool.submit(claim), pool.submit(cancel)
                won, count = claimed.result(), cancelled.result()
            self.assertEqual(count, 0 if won else 1)
            self.assertEqual(db.get_job(job['id'])['status'], 'running' if won else 'cancelled')

    def test_successful_retry_blocks_new_retry_in_transaction(self):
        self.seed(1)
        retry, _ = db.create_retry_job(1, job_type='registration', email_source='outlook')
        db.update_job(retry['id'], status='success')
        with self.assertRaisesRegex(ValueError, '已成功'):
            db.create_retry_job(1, job_type='registration', email_source='outlook')

    def isolate_worker_state(self):
        import threading
        self.stack.enter_context(patch.multiple(svc, _ACTIVE_JOBS=set(), _STOP_EVENTS={},
                                                _THREAD_CTX=threading.local()))
        self.stack.enter_context(patch.object(svc, '_append_job_log'))
        self.stack.enter_context(patch.object(svc, '_release_unconsumed_job_email'))
        self.stack.enter_context(patch.object(svc, '_disable_job_email'))

    def test_worker_skips_terminal_and_deleted_jobs(self):
        self.isolate_worker_state()
        with patch.object(svc, '_prepare_registration_args') as prepare:
            for status in ('cancelled', 'stopped', 'success', 'failed'):
                job = db.create_job('outlook')
                db.update_job(job['id'], status=status)
                svc._run_one_job(job['id'], job['log_file'])
                self.assertEqual(db.get_job(job['id'])['status'], status)
            db.delete_job(job['id'])
            svc._run_one_job(job['id'], job['log_file'])
            prepare.assert_not_called()
        self.assertFalse(svc._ACTIVE_JOBS)

    def test_worker_success_failure_and_stop_lifecycle(self):
        import sys
        import types
        from contextlib import nullcontext
        self.isolate_worker_state()
        fake_main = types.ModuleType('main')
        fake_main.run_registration = MagicMock()
        with patch.dict(sys.modules, {'main': fake_main}), \
             patch.object(svc, '_prepare_registration_args', return_value=('a@example.com', 'name', '2000-01-01')), \
             patch.object(svc, '_JobLogContext', side_effect=lambda _: nullcontext()):
            for outcome, expected in [({'success': True}, 'success'), ({'error': 'failed'}, 'failed')]:
                job = db.create_job('outlook')
                fake_main.run_registration.side_effect = None
                fake_main.run_registration.return_value = outcome
                svc._run_one_job(job['id'], job['log_file'])
                self.assertEqual(db.get_job(job['id'])['status'], expected)
                self.assertFalse(svc._ACTIVE_JOBS)
            job = db.create_job('outlook')
            def stopping(**kwargs):
                self.assertEqual(svc.request_stop_job(job['id'])['state'], 'stopping')
                return {'success': True}
            fake_main.run_registration.side_effect = stopping
            svc._run_one_job(job['id'], job['log_file'])
            self.assertEqual(db.get_job(job['id'])['status'], 'stopped')
            self.assertFalse(svc._STOP_EVENTS)

    def test_stop_does_not_overwrite_concurrent_completion(self):
        import threading
        self.isolate_worker_state()
        original = db.update_job
        for active in (True, False):
            job = db.create_job('outlook')
            db.update_job(job['id'], status='running')
            if active:
                svc._ACTIVE_JOBS.add(job['id'])
                svc._STOP_EVENTS[job['id']] = threading.Event()
            def complete_first(job_id, **kwargs):
                if kwargs.get('status') in ('stopping', 'stopped'):
                    original(job_id, status='success')
                return original(job_id, **kwargs)
            with patch.object(db, 'update_job', side_effect=complete_first):
                self.assertEqual(svc.request_stop_job(job['id'])['state'], 'success')
            self.assertEqual(db.get_job(job['id'])['status'], 'success')

    def test_stop_rechecks_pending_that_was_claimed(self):
        self.isolate_worker_state()
        job = db.create_job('outlook')
        original = db.update_job
        def claim_first(job_id, **kwargs):
            if kwargs.get('status') == 'cancelled':
                self.assertTrue(svc._activate_job(job_id))
            return original(job_id, **kwargs)
        with patch.object(db, 'update_job', side_effect=claim_first):
            self.assertEqual(svc.request_stop_job(job['id'])['state'], 'stopping')
        self.assertTrue(svc.is_stop_requested(job['id']))
        svc._deactivate_job(job['id'])

    def test_duplicate_codex_worker_does_not_release_reservation(self):
        self.isolate_worker_state()
        job = db.create_job('outlook')
        self.assertTrue(svc._activate_job(job['id']))
        with patch.object(svc.codex_retry_service, 'release') as release, \
             patch.object(svc.codex_retry_service, 'run_worker') as worker:
            svc._run_codex_retry_job(job['id'], job['log_file'], 'a@example.com', 1)
            release.assert_not_called()
            worker.assert_not_called()
        svc._deactivate_job(job['id'])

    def test_concurrent_pagination_has_consistent_snapshot(self):
        db.create_jobs(100, 'outlook')
        def write():
            for i in range(1, 101):
                db.update_job(i, status='success')
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(write)
            for _ in range(25):
                page = db.list_jobs_page(limit=100)
                actual = {}
                for job in page['items']:
                    actual[job['status']] = actual.get(job['status'], 0) + 1
                actual['active'] = actual.get('pending', 0)
                self.assertEqual(page['status_counts'], actual)
                self.assertEqual(page['total'], 100)
            future.result()

    def test_duplicate_workers_only_execute_registration_once(self):
        import sys
        import types
        import threading
        from contextlib import nullcontext
        self.isolate_worker_state()
        job = db.create_job('outlook')
        entered, finish = threading.Event(), threading.Event()
        fake_main = types.ModuleType('main')
        def register(**kwargs):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError('test worker timed out')
            return {'success': True}
        fake_main.run_registration = MagicMock(side_effect=register)
        with patch.dict(sys.modules, {'main': fake_main}), \
             patch.object(svc, '_prepare_registration_args', return_value=('a@example.com', 'name', '2000-01-01')), \
             patch.object(svc, '_JobLogContext', side_effect=lambda _: nullcontext()), \
             ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(svc._run_one_job, job['id'], job['log_file'])
            try:
                self.assertTrue(entered.wait(5))
                duplicate = pool.submit(svc._run_one_job, job['id'], job['log_file'])
                duplicate.result(timeout=5)
                self.assertEqual(db.get_job(job['id'])['status'], 'running')
                self.assertIn(job['id'], svc._ACTIVE_JOBS)
            finally:
                finish.set()
            first.result(timeout=5)
        fake_main.run_registration.assert_called_once()
        self.assertEqual(db.get_job(job['id'])['status'], 'success')
        self.assertFalse(svc._ACTIVE_JOBS)
