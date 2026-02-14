# -*- coding: utf-8 -*-
"""
5단계 2차: 페이지 라우트 전용 Blueprint (/, /results, /betting-helper, /practice, 스크립트, favicon).
순환 import 방지: register_pages_routes(app) 호출 시점에 app에서 import.
"""
from flask import Blueprint

pages_bp = Blueprint('pages', __name__)


def register_pages_routes(app):
    """app 로드 완료 후 호출. app의 페이지 뷰 함수를 이 블루프린트에 등록."""
    from app import (
        index,
        results_page,
        betting_helper_page,
        practice_page,
        serve_tampermonkey_script,
        favicon,
    )
    pages_bp.add_url_rule('/', 'index', index, methods=['GET'])
    pages_bp.add_url_rule('/results', 'results_page', results_page, methods=['GET'])
    pages_bp.add_url_rule('/betting-helper', 'betting_helper_page', betting_helper_page, methods=['GET'])
    pages_bp.add_url_rule('/practice', 'practice_page', practice_page, methods=['GET'])
    pages_bp.add_url_rule('/docs/tampermonkey-auto-bet.user.js', 'serve_tampermonkey_script', serve_tampermonkey_script, methods=['GET'])
    pages_bp.add_url_rule('/favicon.ico', 'favicon', favicon, methods=['GET'])
