# -*- coding: utf-8 -*-
"""
2단계 모듈화: 순수 유틸 함수
Flask/request/DB 의존 없음. 입력→출력만.
"""
import re


def sort_results_newest_first(results):
    """결과를 gameID 기준 최신순(높은 ID 먼저)으로 정렬."""
    if not results:
        return results
    def key_fn(r):
        g = str(r.get('gameID') or '')
        nums = re.findall(r'\d+', g)
        n = int(nums[0]) if nums else 0
        return (-n, g)
    return sorted(results, key=key_fn)


def normalize_pick_color_value(color):
    """RED/BLACK 또는 빨강/검정을 일관된 문자열로 통일."""
    if color is None:
        return None
    s = str(color).strip()
    if not s:
        return None
    upper = s.upper()
    if upper in ('RED', '빨강'):
        return '빨강'
    if upper in ('BLACK', '검정'):
        return '검정'
    return s


def flip_pick_color(color):
    """빨강/검정을 서로 반전. 기타 값은 그대로."""
    if color == '빨강':
        return '검정'
    if color == '검정':
        return '빨강'
    return color


def round_eq(a, b):
    """회차 비교: int/str 혼용 시에도 동일 회차면 True."""
    if a is None or b is None:
        return a == b
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return a == b


def parse_card_color(result_str):
    """카드 결과 문자열에서 색상 추출. H,D,♥,♦=빨강 / S,C,♠,♣=검정."""
    if not result_str:
        return None
    s = str(result_str).upper().strip()
    for c in s:
        if c in ('H', 'D') or c in ('♥', '♦'):
            return True
        if c in ('S', 'C') or c in ('♠', '♣'):
            return False
    if 'RED' in s or 'HEART' in s or 'DIAMOND' in s:
        return True
    if 'BLACK' in s or 'SPADE' in s or 'CLUB' in s:
        return False
    return None


def get_card_color_from_result(r):
    """result 객체에서 카드 색상 추출. True=RED, False=BLACK, None=미확인."""
    if not r or r.get('joker'):
        return None
    if r.get('red') and not r.get('black'):
        return True
    if r.get('black') and not r.get('red'):
        return False
    return parse_card_color(r.get('result', ''))
