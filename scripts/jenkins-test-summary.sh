#!/bin/bash
# Jenkins CI/CD - CLI Test Results Summary Script

echo "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   📊 CLI Test Results Summary"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python3 -c "
import json
from pathlib import Path

categories = ['cli', 'runtime', 'framework']
total_tests = 0
total_passed = 0
total_failed = 0
total_errors = 0

print(f'{'Category':15} | {'Tests':5} | {'Passed':6} | {'Failed':6} | {'Errors':6} | {'Coverage':8}')
print(f'{'-'*70}')

for cat in categories:
    report_file = Path(f'test-reports/{cat}/results.json')
    if report_file.exists():
        data = json.loads(report_file.read_text())
        total_tests += data['summary']['total_tests']
        total_passed += data['summary']['passed']
        total_failed += data['summary']['failed']
        total_errors += data['summary']['errors']
        
        status = '✅' if data['success'] else '❌'
        print(f'{status} {cat:15} | {data[\"summary\"][\"total_tests\"]:5} | {data[\"summary\"][\"passed\"]:6} | {data[\"summary\"][\"failed\"]:6} | {data[\"summary\"][\"errors\"]:6} | {data[\"coverage\"][\"percentage\"]:6.1f}%')

print(f'{'-'*70}')
print(f'{'TOTAL':17} | {total_tests:5} | {total_passed:6} | {total_failed:6} | {total_errors:6} |')
print(f'{'-'*70}')

if total_failed > 0 or total_errors > 0:
    print(f'\n❌ Test failures detected!')
    exit(1)
else:
    print(f'\n✅ All CLI tests passed!')
"
