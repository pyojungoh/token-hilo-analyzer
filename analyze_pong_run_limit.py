# -*- coding: utf-8 -*-
"""퐁당 run 길이별 "다음 결과" 분포 분석 — 장퐁당(끊김) 임계값 파악용

CSV graph_analysis에서:
- 구간=pong_phase일 때 퐁당run 첫 숫자 = 현재 퐁당 run 길이
- 그 길이(3, 4, 5, 6...)별로 다음 회차 실제(정/꺽) 분포 집계
- 정이 더 많이 나오면 → 줄로 전환됨 → line_w 가산(장퐁당 끊김 예상)
- 꺽이 더 많이 나오면 → 퐁당 유지 → pong_w 유지

사용: python analyze_pong_run_limit.py [CSV경로]
"""
import csv
import sys
from collections import defaultdict

def first_pong_run(s):
    """퐁당run 컬럼 '3,4,2' → 3 (첫 숫자 = 현재 퐁당 run 길이)"""
    if not s or s == '-':
        return None
    parts = str(s).strip().split(',')
    if not parts:
        return None
    try:
        return int(parts[0].strip())
    except ValueError:
        return None

def main(path):
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip().startswith('#'):
                continue
            r = next(csv.reader([line]))
            if len(r) >= 12 and r[0] != '회차':
                try:
                    int(r[0])
                    rows.append(r)
                except ValueError:
                    pass

    # 실제(정/꺽)만
    valid = [r for r in rows if len(r) > 2 and r[2] in ('정', '꺽')]
    n = len(valid)

    # pong_phase일 때 현재 퐁당 run 길이별 → 다음 회차 실제(정/꺽) 집계
    # col 7: 구간, col 11: 퐁당run
    by_pong_len = defaultdict(lambda: {'정': 0, '꺽': 0})
    for i in range(len(valid) - 1):
        r = valid[i]
        r_next = valid[i + 1]
        if len(r) < 12:
            continue
        phase = (r[7] or '').strip()
        if phase != '퐁당구간':  # CSV는 한글 구간명
            continue
        plen = first_pong_run(r[11])
        if plen is None:
            continue
        actual = r_next[2]
        if actual in ('정', '꺽'):
            by_pong_len[plen][actual] += 1

    print('=' * 60)
    print('퐁당 run 길이별 "다음 회차" 실제(정/꺽) 분포')
    print('=' * 60)
    print(f'분석 대상: pong_phase 구간 {sum(by_pong_len[k]["정"]+by_pong_len[k]["꺽"] for k in by_pong_len)}회')
    print()
    print('| 퐁당run길이 | 정 % | 꺽 % | 총 | 해석 |')
    print('|------------|------|------|-----|------|')

    for plen in sorted(by_pong_len.keys()):
        d = by_pong_len[plen]
        t = d['정'] + d['꺽']
        if t < 5:
            continue
        j = 100 * d['정'] / t
        k = 100 * d['꺽'] / t
        if j >= 55:
            rec = '정↑ 줄 전환 예상 → line_w 가산'
        elif k >= 55:
            rec = '꺽↑ 퐁당 유지 → pong_w 유지'
        else:
            rec = '중립'
        print(f'| {plen} | {j:.1f} | {k:.1f} | {t} | {rec} |')

    # 장퐁당 임계값 제안: 정이 52% 이상 나오기 시작하는 최소 길이
    print()
    print('--- 장퐁당(끊김) 임계값 제안 ---')
    candidates = []
    for plen in sorted(by_pong_len.keys(), reverse=True):
        d = by_pong_len[plen]
        t = d['정'] + d['꺽']
        if t < 15:
            continue
        j = 100 * d['정'] / t
        if j >= 52:
            candidates.append((plen, j, t))
    if candidates:
        best = min(candidates, key=lambda x: x[0])  # 가장 낮은 길이에서 정 52%+
        print(f'  퐁당 run ≥ {best[0]}일 때 정 {best[1]:.1f}% ({best[2]}회) → 줄 전환 예상')
        print(f'  → 장퐁당 임계값: pong_runs[0] >= {best[0]} 시 line_w 가산 권장')
    else:
        print('  샘플 부족 또는 정 52%+ 구간 없음. 기본 5 유지 권장.')
    print('=' * 60)

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else r'c:\Users\pyo08\Downloads\graph_analysis_2026-02-27 (9).csv'
    main(path)
