"""Gunicorn 설정 파일 - Socket.IO 초기화"""
import os
import sys

# app 모듈 import
from app import init_socketio

def on_starting(server):
    """Gunicorn 서버 시작 시 실행"""
    print("=" * 50)
    print("[Gunicorn] 서버 시작 중...")
    print("=" * 50)
    # Socket.IO 초기화
    init_socketio()
