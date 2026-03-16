#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
그래프 결과 지연 진단 스크립트.
로컬 서버(http://127.0.0.1:5000)가 떠 있어야 함.
"""
import time
import requests

BASE = 'http://127.0.0.1:5000'
N = 20

def main():
    print(f"[진단] {BASE}/api/results {N}회 연속 요청 (100ms 간격)...")
    times = []
    cache_hits = 0
    for i in range(N):
        t0 = time.perf_counter()
        try:
            r = requests.get(f'{BASE}/api/results', timeout=5)
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
            if elapsed < 50:
                cache_hits += 1
        except Exception as e:
            print(f"  요청 {i+1} 실패: {e}")
        time.sleep(0.1)
    if not times:
        print("  응답 없음. 서버가 실행 중인지 확인하세요.")
        return
    avg = sum(times) / len(times)
    print(f"\n[결과] 평균 {avg:.0f}ms, 최소 {min(times):.0f}ms, 최대 {max(times):.0f}ms")
    print(f"  캐시 히트 추정(<50ms): {cache_hits}/{N} ({100*cache_hits/N:.0f}%)")
    if avg > 100:
        print("  ⚠ 캐시 미스 빈번 — RESULTS_RESPONSE_CACHE_TTL_MS 또는 apply 부하 확인")
    print("\n서버 로그에서 PERF_PROFILE=1 시 build_results_payload_db_only, scheduler_apply ms 확인")

if __name__ == '__main__':
    main()
