# -*- coding: utf-8 -*-
"""
5단계: app.py에 API + pages Blueprint 등록 블록 추가.
if __name__ == '__main__': 앞에 삽입. (한 줄씩 처리, OOM 방지)
실행: python step5_add_pages_blueprint.py
"""
MARKER = "if __name__ == '__main__':"
BLOCK_TO_ADD = """from routes_api import api_bp, register_api_routes
app.register_blueprint(api_bp)
register_api_routes(app)
from routes_pages import pages_bp, register_pages_routes
app.register_blueprint(pages_bp)
register_pages_routes(app)

"""

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    inserted = False
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for line in f:
                if not inserted and MARKER in line:
                    out.write(BLOCK_TO_ADD)
                    inserted = True
                out.write(line)
    if not inserted:
        print("Warning: marker not found, no change made.")
        import os
        try:
            os.remove(out_path)
        except Exception:
            pass
        return
    import os
    os.replace(out_path, app_path)
    print("Done. API + pages Blueprint registration added to app.py.")

if __name__ == '__main__':
    main()
