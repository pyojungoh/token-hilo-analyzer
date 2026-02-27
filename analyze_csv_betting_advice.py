# -*- coding: utf-8 -*-
"""CSV 배팅 어드바이스 분석 (docs/CSV_BETTING_ADVICE_GUIDE.md 기준)"""
import csv
import sys
from collections import defaultdict

def parse_wr(s):
    try:
        return float(str(s).replace('%','').strip())
    except:
        return None

def main(path):
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip().startswith('#'):
                continue
            r = next(csv.reader([line]))
            if len(r) >= 5 and r[0] != '회차':
                try:
                    int(r[0])
                    rows.append(r)
                except ValueError:
                    pass

    # 실제(정/꺽)만 있는 행 (joker 제외)
    valid = [r for r in rows if len(r) > 2 and r[2] in ('정', '꺽')]
    n = len(valid)

    # === 1. 모양별 "다음 회차" 실제 결과(정/꺽) 집계 ===
    # row i의 모양일 때 → row i+1의 실제가 정/꺽 중 어느 쪽인지
    def next_result_by_key(key_idx, datalist):
        by_key = defaultdict(lambda: {'정': 0, '꺽': 0})
        for i in range(len(datalist) - 1):
            r = datalist[i]
            r_next = datalist[i + 1]
            if len(r) <= key_idx or len(r_next) < 3:
                continue
            k = r[key_idx] if r[key_idx] else '-'
            actual = r_next[2]
            if actual in ('정', '꺽'):
                by_key[k][actual] += 1
        return by_key

    # 구간별 다음 결과
    by_phase_next = next_result_by_key(7, valid[-800:])
    by_shape_next = next_result_by_key(9, valid[-800:])

    # 구간+모양 조합 (자주 나오는 것)
    recent = valid[-800:]
    by_combo = defaultdict(lambda: {'정': 0, '꺽': 0})
    for i in range(len(recent) - 1):
        r, r_next = recent[i], recent[i + 1]
        if len(r) < 10 or len(r_next) < 3:
            continue
        phase = r[7] if r[7] else '-'
        shape = r[9] if r[9] else '-'
        if phase == '-' and shape == '-':
            continue
        combo = f"{phase}|{shape}"
        actual = r_next[2]
        if actual in ('정', '꺽'):
            by_combo[combo][actual] += 1

    # === 2. 경고승률 구간별 정픽승률 vs 반픽승률 ===
    def band(wr):
        if wr is None: return None
        if wr <= 43: return '≤43%'
        if wr < 50: return '43~50%'
        if wr < 57: return '50~57%'
        return '≥57%'

    bands = ['≤43%', '43~50%', '50~57%', '≥57%']
    band_stats = {b: {'정픽승': 0, '정픽전체': 0, '반픽승': 0, '반픽전체': 0} for b in bands}

    for r in valid[-800:]:
        if len(r) < 5:
            continue
        wr = parse_wr(r[4])
        b = band(wr)
        if b is None:
            continue
        actual = r[2]
        pick = r[1]
        if actual not in ('정', '꺽'):
            continue
        # 정픽: 픽=실제
        if pick == actual:
            band_stats[b]['정픽승'] += 1
        band_stats[b]['정픽전체'] += 1
        # 반픽: 픽≠실제 (반대로 픽했을 때)
        rev_pick = '꺽' if pick == '정' else '정'
        if rev_pick == actual:
            band_stats[b]['반픽승'] += 1
        band_stats[b]['반픽전체'] += 1

    # === 출력 ===
    print('='*70)
    print('CSV 배팅 어드바이스 분석 (graph_analysis_2026-02-27 (9).csv)')
    print('='*70)
    print(f'분석 대상: {n}회 (조커 제외)')

    print('\n--- 1. 구간별 "다음 회차" 실제 결과(정/꺽) 비율 (최근 800) ---')
    for p in sorted(by_phase_next.keys(), key=lambda x: -(by_phase_next[x]['정']+by_phase_next[x]['꺽']))[:12]:
        d = by_phase_next[p]
        t = d['정'] + d['꺽']
        if t < 10:
            continue
        j = 100 * d['정'] / t
        k = 100 * d['꺽'] / t
        rec = '정 권장' if j >= 55 else ('꺽 권장' if k >= 55 else '중립')
        print(f'  {p}: 정 {j:.1f}% / 꺽 {k:.1f}% ({t}회) → {rec}')

    print('\n--- 2. 덩어리모양별 "다음 회차" 실제 결과(정/꺽) 비율 (최근 800) ---')
    for s in sorted(by_shape_next.keys(), key=lambda x: -(by_shape_next[x]['정']+by_shape_next[x]['꺽']))[:10]:
        d = by_shape_next[s]
        t = d['정'] + d['꺽']
        if t < 15:
            continue
        j = 100 * d['정'] / t
        k = 100 * d['꺽'] / t
        rec = '정 권장' if j >= 55 else ('꺽 권장' if k >= 55 else '중립')
        print(f'  {s}: 정 {j:.1f}% / 꺽 {k:.1f}% ({t}회) → {rec}')

    print('\n--- 3. 경고승률 구간별 정픽승률 vs 반픽승률 (스마트반픽 임계값) ---')
    reverse_ok_bands = []
    for b in bands:
        d = band_stats[b]
        if d['정픽전체'] == 0:
            continue
        j = 100 * d['정픽승'] / d['정픽전체']
        r = 100 * d['반픽승'] / d['반픽전체']
        rev_ok = r > j
        if rev_ok:
            reverse_ok_bands.append(b)
        print(f'  {b}: 정픽 {j:.1f}% / 반픽 {r:.1f}% ({d["정픽전체"]}회) → {"반픽 유리" if rev_ok else "정픽 유리"}')

    if reverse_ok_bands:
        print(f'\n  → 스마트반픽 임계값 권장: {reverse_ok_bands[-1]} 상한 (반픽 유리 구간 상한)')
    else:
        print('\n  → 반픽 유리 구간 없음. 스마트반픽 임계값 낮게 유지 권장.')

    # 최근 상태
    last = valid[-1]
    print('\n--- 4. 현재(최근) 상태 ---')
    print(f'  회차: {last[0]}, 픽: {last[1]}, 실제: {last[2]}, 승패: {last[3]}')
    print(f'  경고승률: {last[4]}, 구간: {last[7] if len(last)>7 else "-"}, 덩어리모양: {last[9] if len(last)>9 else "-"}')
    print(f'  줄%: {last[12] if len(last)>12 else "-"}, 퐁당%: {last[13] if len(last)>13 else "-"}')

    # 최근 5행 패턴 → 다음 픽 권장
    recent_phase = last[7] if len(last) > 7 else '-'
    recent_shape = last[9] if len(last) > 9 else '-'
    if recent_phase in by_phase_next:
        d = by_phase_next[recent_phase]
        t = d['정'] + d['꺽']
        if t >= 10:
            j = 100 * d['정'] / t
            k = 100 * d['꺽'] / t
            rec = '정' if j >= 55 else ('꺽' if k >= 55 else '중립')
            print(f'\n  현재 구간 "{recent_phase}" → 다음 회차 {rec} 픽 권장 (정{j:.1f}%/꺽{k:.1f}%)')
    if recent_shape in by_shape_next:
        d = by_shape_next[recent_shape]
        t = d['정'] + d['꺽']
        if t >= 15:
            j = 100 * d['정'] / t
            k = 100 * d['꺽'] / t
            rec = '정' if j >= 55 else ('꺽' if k >= 55 else '중립')
            print(f'  현재 모양 "{recent_shape}" → 다음 회차 {rec} 픽 권장 (정{j:.1f}%/꺽{k:.1f}%)')

    print('\n' + '='*70)

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else r'c:\Users\pyo08\Downloads\graph_analysis_2026-02-27 (9).csv'
    main(path)
