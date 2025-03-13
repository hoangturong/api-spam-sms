# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
import subprocess
import threading
import datetime
import logging
import time
import os
import json
import signal
from threading import Lock

app = Flask(__name__)

# Thiết lập logging với hỗ trợ Unicode
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

USER_FILE_PATH = 'user.txt'
MAX_SLOTS_PER_KEY = 5
MAX_EXECUTION_TIME = 120  # Giới hạn thời gian tối đa 120s
running_tasks = {}  # Lưu trữ các task đang chạy theo key
phone_spam_status = {}  # Lưu trạng thái spam của số điện thoại
slots_lock = Lock()  # Lock để đồng bộ truy cập slots

def read_user_file(file_path):
    """Đọc file user.txt để kiểm tra API key và ngày hết hạn."""
    user_data = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            for line in file:
                try:
                    key, expiration_date = line.strip().split(':')
                    user_data[key] = expiration_date
                except ValueError:
                    logger.warning(f"Dòng không hợp lệ trong file user.txt: {line.strip()}")
    except FileNotFoundError:
        logger.error(f"Không tìm thấy file: {file_path}")
    return user_data

def validate_api_key(key):
    """Kiểm tra tính hợp lệ của API key."""
    user_data = read_user_file(USER_FILE_PATH)
    if key in user_data:
        expiration_date = user_data[key]
        try:
            current_date = datetime.datetime.now().date()
            exp_date = datetime.datetime.strptime(expiration_date, '%Y-%m-%d').date()
            if current_date <= exp_date:
                return True, expiration_date
            else:
                logger.info(f"API key hết hạn: {key}")
        except ValueError:
            logger.error(f"Định dạng ngày không hợp lệ cho key {key}: {expiration_date}")
    else:
        logger.info(f"API key không hợp lệ: {key}")
    return False, None

def authenticate_key(func):
    """Decorator để xác thực API key."""
    def wrapper(*args, **kwargs):
        key = request.args.get("key")
        if not key:
            return jsonify({"error": "API key is required"}), 403
        is_valid, key_info = validate_api_key(key)
        if not is_valid:
            return jsonify({"error": "Invalid or expired API key"}), 403
        return func(*args, key_info=key_info, **kwargs)
    return wrapper

def initialize_slots_for_key(key):
    """Khởi tạo slots cho key nếu chưa tồn tại."""
    with slots_lock:
        if key not in running_tasks:
            running_tasks[key] = {
                'slots': MAX_SLOTS_PER_KEY,  # Số slots khả dụng
                'active_tasks': 0,           # Số task đang chạy
                'phones': {}                 # Lưu trạng thái các số điện thoại
            }
        logger.debug(f"Slots initialized for key {key}: {running_tasks[key]}")

def get_slots_for_key(key):
    """Lấy số slots còn lại cho key."""
    with slots_lock:
        initialize_slots_for_key(key)
        available_slots = running_tasks[key]['slots']
        logger.debug(f"Available slots for key {key}: {available_slots}")
        return available_slots

def update_slots_for_key(key, increment=True):
    """Cập nhật số slots cho key: tăng hoặc giảm."""
    with slots_lock:
        initialize_slots_for_key(key)
        if increment:
            running_tasks[key]['slots'] = min(running_tasks[key]['slots'] + 1, MAX_SLOTS_PER_KEY)
            running_tasks[key]['active_tasks'] = max(running_tasks[key]['active_tasks'] - 1, 0)
        else:
            running_tasks[key]['slots'] = max(running_tasks[key]['slots'] - 1, 0)
            running_tasks[key]['active_tasks'] = min(running_tasks[key]['active_tasks'] + 1, MAX_SLOTS_PER_KEY)
        logger.debug(f"Updated slots for key {key}: slots={running_tasks[key]['slots']}, active_tasks={running_tasks[key]['active_tasks']}")

def is_phone_spamming(phone):
    """Kiểm tra xem số điện thoại đang được spam hay không."""
    if phone in phone_spam_status:
        start_time, max_time = phone_spam_status[phone]
        if time.time() - start_time < max_time:
            return True
        else:
            del phone_spam_status[phone]  # Xóa trạng thái nếu hết thời gian
    return False

@app.route('/api', methods=['GET'])
@authenticate_key
def execute_spam(key_info):
    """Xử lý yêu cầu API để thực thi các phương thức SMS, CALL, FREE."""
    try:
        methods = request.args.get('methods', '')
        phone = request.args.get('phone', '')
        time_str = request.args.get('time', '')

        if not all([methods, phone, time_str]):
            return jsonify({"Status": "error", "Noti": "Vui lòng nhập đầy đủ thông tin: methods, phone, time"}), 400

        try:
            max_execution_time = min(int(time_str), MAX_EXECUTION_TIME)
            if max_execution_time <= 0:
                return jsonify({"Status": "error", "Noti": "Thời gian phải là số nguyên dương"}), 400
        except ValueError:
            return jsonify({"Status": "error", "Noti": "Thời gian phải là số nguyên hợp lệ"}), 400

        valid_methods = ["SMS", "CALL", "FREE"]
        if methods not in valid_methods:
            return jsonify({"Status": "error", "Noti": "Phương thức không hợp lệ, chỉ hỗ trợ FREE, SMS hoặc CALL"}), 400

        key = request.args.get("key")
        with slots_lock:
            initialize_slots_for_key(key)
            if running_tasks[key]['slots'] <= 0 or running_tasks[key]['active_tasks'] >= MAX_SLOTS_PER_KEY:
                return jsonify({"Status": "error", "Noti": "Đã hết slots cho key này, vui lòng đợi"}), 429

        if is_phone_spamming(phone):
            return jsonify({"Status": "error", "Noti": f"Số điện thoại {phone} đang được spam, vui lòng thử lại sau"}), 429

        def execute_command(stop_event):
            """Thực thi lệnh CMD cho script con trong thread riêng."""
            process = None
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                if methods == "FREE":
                    cmd = f'python "{os.path.join(script_dir, "sms.py")}" "{phone}" {max_execution_time} 2 10'
                elif methods == "CALL":
                    cmd = f'python "{os.path.join(script_dir, "sms-1.py")}" "{phone}" 5'
                elif methods == "SMS":
                    cmd = f'python "{os.path.join(script_dir, "sms-2.py")}" "{phone}" 5'

                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    text=True,
                    cwd=script_dir,
                    encoding='utf-8',
                    errors='replace',
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=os.setsid if os.name != 'nt' else None
                )

                start_time = time.time()
                phone_spam_status[phone] = (start_time, max_execution_time)
                update_slots_for_key(key, increment=False)

                while process.poll() is None and not stop_event.is_set():
                    time.sleep(0.1)

                if stop_event.is_set():
                    if os.name != 'nt' and process.pid:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            time.sleep(0.5)
                            if process.poll() is None:
                                os.killpg(process.pid, signal.SIGKILL)
                        except Exception as e:
                            logger.error(f"Lỗi khi dừng process group: {str(e)}")
                    else:
                        process.terminate()
                        time.sleep(0.5)
                        if process.poll() is None:
                            process.kill()
                    logger.info(f"Đã dừng thread cho {methods} - {phone} sau {max_execution_time}s")
                else:
                    stdout, stderr = process.communicate()
                    if stdout:
                        logger.info(f"Output từ script: {stdout}")
                    if stderr:
                        logger.error(f"Lỗi từ script: {stderr}")
                    logger.info(f"Script {methods} cho {phone} hoàn thành tự nhiên sau {time.time() - start_time:.2f}s")

            except Exception as e:
                logger.error(f"Lỗi khi thực thi lệnh CMD {methods}: {str(e)}")
            finally:
                if process and process.poll() is None:
                    try:
                        if os.name != 'nt':
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        logger.info(f"Đã buộc kết thúc process cho {methods} - {phone}")
                    except Exception as e:
                        logger.error(f"Lỗi khi buộc dừng process: {str(e)}")
                update_slots_for_key(key, increment=True)
                if phone in phone_spam_status and time.time() - phone_spam_status[phone][0] >= phone_spam_status[phone][1]:
                    del phone_spam_status[phone]

        stop_event = threading.Event()
        execution_thread = threading.Thread(target=execute_command, args=(stop_event,), daemon=True)
        execution_thread.start()

        def stop_after_timeout():
            time.sleep(max_execution_time)
            stop_event.set()
            logger.info(f"Hết thời gian {max_execution_time}s, yêu cầu dừng thread cho {phone}")

        threading.Thread(target=stop_after_timeout, daemon=True).start()

        result = {
            "Status": "Success",
            "time": time_str,
            "phone": phone,
            "Methods": methods,
            "Owner": "Van_trong",
            "key": key_info,
            "max_execution_time": max_execution_time,
            "remaining_slots": get_slots_for_key(key)
        }
        return app.response_class(
            response=json.dumps(result, indent=4, ensure_ascii=False),
            status=200,
            mimetype='application/json'
        )

    except Exception as e:
        logger.error(f"Lỗi máy chủ: {str(e)}")
        return jsonify({'error': 'Lỗi máy chủ nội bộ'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)