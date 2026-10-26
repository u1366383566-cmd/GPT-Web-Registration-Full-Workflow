"""注册任务本地基准：临时 SQLite、模拟线程池，不注册账号或请求外部服务。

python tools/benchmark_registration_jobs.py --history 5000 --count 50
python tools/benchmark_registration_jobs.py --baseline-ref <优化前commit>
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import db, registration_service


def load_baseline(ref):
    modules = []
    for filename in ('db.py', 'registration_service.py'):
        name = 'benchmark_baseline_' + filename[:-3]
        module = types.ModuleType(name)
        module.__file__ = str(ROOT / 'core' / filename)
        source = subprocess.check_output(['git', 'show', f'{ref}:core/{filename}'], cwd=ROOT)
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
        modules.append(module)
    modules[1].db = modules[0]
    return modules


def benchmark(database, service, history, count, page_size):
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        missing = root / 'missing.json'
        paths = {name: missing for name in (
            '_OUTLOOK_JSON', '_GENERIC_API_EMAIL_JSON', '_DOMAIN_EMAIL_JSON',
            '_LEGACY_OUTLOOK_JSON', '_LEGACY_ACCOUNTS_JSON', '_LEGACY_JOBS_JSON',
            '_LEGACY_CODEX_EXPORT_STATE',
        )}
        paths.update(_ACCOUNTS_JSON=root/'accounts.json', _JOBS_JSON=root/'jobs.json',
                     _LEGACY_SQLITE=root/'missing.db', _LOG_DIR=root/'logs',
                     _CODEX_DIR=root/'codex', _CODEX_AGENT_DIR=root/'agent',
                     _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        stack.enter_context(patch.multiple(database, **paths))
        database._ensure_sqlite()
        database._save_jobs([
            {'id':i+1, 'email':f'user{i}@example.com', 'status':'failed',
             'email_source':'outlook', 'error_message':'benchmark', 'created_at':'2026-01-01'}
            for i in range(history)
        ])
        stack.enter_context(patch.object(service, 'get_executor', return_value=MagicMock()))
        stack.enter_context(patch.object(service, 'get_executor_workers', return_value=3))
        started = time.perf_counter()
        jobs = service.submit_registration(count=count, email_source='outlook', workers=3)
        submit_ms = (time.perf_counter()-started)*1000
        started = time.perf_counter()
        for job in jobs:
            database.update_job(job['id'], status='running', email='benchmark@example.com')
            database.update_job(job['id'], network_traffic={'bytes': 1024})
            database.update_job(job['id'], status='success')
        update_ms = (time.perf_counter()-started)*1000
        statements = []
        connect = database._sqlite_conn
        def traced_connection():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection
        # 跳过刚创建的 pending 任务，确保测试包含终态任务的重试能力查询。
        started = time.perf_counter()
        with patch.object(database, '_sqlite_conn', side_effect=traced_connection):
            page = database.list_jobs_page(limit=page_size, offset=count)
            if hasattr(service, 'enrich_jobs_retry_info'):
                service.enrich_jobs_retry_info(page['items'])
            else:
                for job in page['items']:
                    job.update(service.get_retry_info(job))
            if 'status_counts' not in page:
                page['status_counts'] = database.job_status_counts()
        page_ms = (time.perf_counter()-started)*1000
        assert len(jobs) == count and page['total'] == history+count
        return {'history':history, 'created':count, 'page_size':page_size,
                'submit_ms':round(submit_ms,2), 'page_ms':round(page_ms,2),
                'status_updates':count*3, 'update_ms':round(update_ms,2),
                'page_selects':sum(q.lstrip().upper().startswith('SELECT') for q in statements)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=int, default=5000)
    parser.add_argument('--count', type=int, default=50)
    parser.add_argument('--page-size', type=int, default=50)
    parser.add_argument('--baseline-ref')
    args = parser.parse_args()
    if min(args.history, args.count, args.page_size) <= 0:
        parser.error('history/count/page-size 必须大于 0')
    result = {}
    if args.baseline_ref:
        old_db, old_service = load_baseline(args.baseline_ref)
        result['baseline'] = benchmark(old_db, old_service, args.history, args.count, args.page_size)
    result['current'] = benchmark(db, registration_service, args.history, args.count, args.page_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
