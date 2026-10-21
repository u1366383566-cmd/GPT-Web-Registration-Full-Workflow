"""本地存储基准：使用临时数据库，不请求网络、不修改正式账号。

运行：.venv/bin/python tests/benchmark_account_inspection.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_account_inspection import AccountInspectionTests, jwt
from core import db, account_inspection_service as service


def main():
    case = AccountInspectionTests()
    try:
        case.setUp()
        token = jwt({'exp': 9999999999})
        with db._LOCK:
            db._save_accounts([
                {'id': i, 'email': f'bench-{i}@example.test', 'access_token': token, 'note': 'x' * 200}
                for i in range(1, 1001)
            ])
        start = time.perf_counter()
        for i in range(1, 1001):
            db.get_account(i)
        read = time.perf_counter() - start

        start = time.perf_counter()
        for i in range(1, 101):
            run_id = str(i)
            assert db.claim_account_live_check(i, run_id=run_id)
            assert db.mark_account_live_check_running(i, run_id=run_id)
            assert db.update_account_liveness(i, {'ok': True, 'access_token': token}, expected_run_id=run_id)
            assert db.claim_account_plan_check(i, run_id=run_id)
            assert db.mark_account_plan_check_running(i, run_id=run_id)
            assert db.update_account_plan_check(
                i, result={'ok': True, 'current_plan_type': 'plus'}, expected_run_id=run_id,
            )
        lifecycle = time.perf_counter() - start

        run, _ = db.create_account_inspection(list(range(1, 1001)), workers=8)
        start = time.perf_counter()
        service._run(run)
        scan = time.perf_counter() - start
        assert run['checked_count'] == 1000 and run['status'] == 'completed'
        print(json.dumps(dict(read_1000=read, lifecycle_100=lifecycle, valid_inspection_1000=scan)))
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
