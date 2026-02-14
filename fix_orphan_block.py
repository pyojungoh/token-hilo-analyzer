# -*- coding: utf-8 -*-
"""app.py 85~149줄 고아 블록 제거 (init_database 몸통만 남은 부분). 한 줄씩 처리."""
def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    skip = set(range(85, 150))  # 85~149 제거 (1-based)
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for i, line in enumerate(f, start=1):
                if i in skip:
                    continue
                out.write(line)
    import os
    os.replace(out_path, app_path)
    print('Done. Orphan block (lines 85-149) removed.')

if __name__ == '__main__':
    main()
