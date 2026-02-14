# -*- coding: utf-8 -*-
"""app.py에 apply_logic import 한 줄만 추가. prediction_logic import 다음에 삽입. 한 줄씩 처리."""
MARKER = "from prediction_logic import"
LINE_TO_ADD = "from apply_logic import _apply_results_to_calcs, _server_calc_effective_pick_and_amount\n"

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    added = False
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for line in f:
                out.write(line)
                if not added and MARKER in line:
                    out.write(LINE_TO_ADD)
                    added = True
    if not added:
        print("Warning: marker not found.")
        import os
        try: os.remove(out_path)
        except: pass
        return
    import os
    os.replace(out_path, app_path)
    print("Done. apply_logic import added to app.py.")

if __name__ == '__main__':
    main()
