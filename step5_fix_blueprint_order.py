# -*- coding: utf-8 -*-
"""
Blueprint 등록 순서 수정: add_url_rule 먼저, register_blueprint 나중.
한 줄씩 읽어서 기존 블록만 올바른 순서로 교체 (OOM 방지).
"""
OLD_BLOCK = """from routes_api import api_bp, register_api_routes
app.register_blueprint(api_bp)
register_api_routes(app)
from routes_pages import pages_bp, register_pages_routes
app.register_blueprint(pages_bp)
register_pages_routes(app)
"""

NEW_BLOCK = """from routes_api import api_bp, register_api_routes
from routes_pages import pages_bp, register_pages_routes
register_api_routes(app)
app.register_blueprint(api_bp)
register_pages_routes(app)
app.register_blueprint(pages_bp)
"""

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    start_marker = "from routes_api import api_bp"
    replaced = False
    skip_count = 0
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for line in f:
                if not replaced and start_marker in line:
                    out.write(NEW_BLOCK)
                    replaced = True
                    skip_count = 7
                    continue
                if skip_count > 0:
                    skip_count -= 1
                    continue
                out.write(line)
    if not replaced:
        print("Warning: block not found. No change.")
        import os
        try:
            os.remove(out_path)
        except Exception:
            pass
        return
    import os
    os.replace(out_path, app_path)
    print("Done. Blueprint order fixed.")

if __name__ == '__main__':
    main()
