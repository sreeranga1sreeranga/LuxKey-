import os
import sys
import json
import time
import base64
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from io import BytesIO
from dotenv import load_dotenv
from PIL import Image
from pynput import keyboard, mouse

# Load environment variables from .env
load_dotenv()

luxkey_active = True
is_scanning = False
mouse_controller = mouse.Controller()

# --- 1. Hardware Caps Lock LED Control ---
def set_led(state: int):
    """Calls ./led_control 1 (on) or 0 (off)"""
    try:
        subprocess.run(
            ["./led_control", str(state)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[LuxKey] LED binary error: {e}", flush=True)

def blink_sync(count: int, on_time: float, off_time: float):
    """Synchronous blink helper so scan flash and answer blinks never overlap"""
    for i in range(count):
        set_led(1)
        time.sleep(on_time)
        set_led(0)
        if i < count - 1:
            time.sleep(off_time)

def led_toggle_on():
    threading.Thread(target=blink_sync, args=(2, 0.12, 0.12), daemon=True).start()

def led_toggle_off():
    threading.Thread(target=blink_sync, args=(1, 0.75, 0.10), daemon=True).start()

def led_scan_started():
    blink_sync(count=1, on_time=0.10, off_time=0.05)

def led_answer(option_letter: str):
    mapping = {"A": 1, "B": 2, "C": 3, "D": 4}
    blinks = mapping.get(option_letter.strip().upper(), 0)
    if blinks:
        # Distinct, steady blinks (0.45s ON, 0.35s OFF) so 1, 2, 3, or 4 is easy to count
        blink_sync(count=blinks, on_time=0.45, off_time=0.35)
    else:
        led_error()

def led_error():
    # 5 very fast flashes (0.06s) for error / no question
    blink_sync(count=5, on_time=0.06, off_time=0.06)


# --- 2. Multi-Ratio Screen Capture (Quadrant + Vertical + Horizontal) ---
def image_to_base64(img: Image.Image, max_dim: int = 1400) -> str:
    """Resizes image cleanly for fast OCR/Vision and encodes as base64 PNG"""
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def capture_multi_views():
    """
    Captures the macOS screen and creates 3 targeted views:
    1. Cursor Quadrant / Hovered Block (tight crop around mouse pointer)
    2. Vertical Center Strip (for vertically stacked quiz cards)
    3. Full Horizontal / Screen View (for wide or side-by-side layouts)
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        res = subprocess.run(["screencapture", "-x", tmp_path], capture_output=True, text=True)
        if res.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError(
                "macOS blocked screen capture. Enable Screen Recording for VS Code/Terminal in System Settings."
            )

        full_img = Image.open(tmp_path).convert("RGB")
        img_w, img_h = full_img.size

        # Get mouse cursor position (in logical points) and scale to Retina pixels
        mx, my = mouse_controller.position
        # Estimate Retina scale factor (usually 2.0 on MacBook Air)
        scale_x = 2.0 if img_w > 2000 else 1.0
        scale_y = 2.0 if img_h > 1300 else 1.0
        px = min(max(int(mx * scale_x), 0), img_w)
        py = min(max(int(my * scale_y), 0), img_h)

        # Crop 1: Hovered Region / Quadrant around cursor (55% width x 65% height centered on cursor)
        half_qw = int(img_w * 0.275)
        half_qh = int(img_h * 0.325)
        left_q = max(0, min(px - half_qw, img_w - 2 * half_qw))
        top_q = max(0, min(py - half_qh, img_h - 2 * half_qh))
        cursor_crop = full_img.crop((left_q, top_q, min(img_w, left_q + 2 * half_qw), min(img_h, top_q + 2 * half_qh)))

        # Crop 2: Vertical Center Strip (middle 60% width, full height - ideal for vertical quizzes)
        v_left = int(img_w * 0.20)
        v_right = int(img_w * 0.80)
        vertical_crop = full_img.crop((v_left, int(img_h * 0.10), v_right, int(img_h * 0.92)))

        return [
            image_to_base64(cursor_crop, max_dim=1200),
            image_to_base64(vertical_crop, max_dim=1200),
            image_to_base64(full_img, max_dim=1400),
        ]
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# --- 3. Vision AI Analyzer (Supports Gemini, Groq, and OpenAI from .env) ---
SYSTEM_PROMPT = """You are a precise multiple-choice question solver analyzing macOS screen captures.
You are given 3 crops of the user's screen:
- Image 1: The block/quadrant directly under the user's mouse cursor.
- Image 2: The vertical center strip of the screen.
- Image 3: The full horizontal screen view.

INSTRUCTIONS:
1. Locate the active multiple-choice question (prioritize Image 1 if a complete question is inside it; otherwise use Image 2 or Image 3).
2. Options may be arranged VERTICALLY (stacked top-to-bottom), HORIZONTALLY (left-to-right), or in a 2x2 GRID, and they often DO NOT have 'A', 'B', 'C', 'D' written next to them.
3. Map the 4 choices to letters strictly by their position:
   - Vertical stack (top to bottom): 1st choice = A, 2nd choice = B, 3rd choice = C, 4th choice = D.
   - Horizontal row (left to right): 1st choice = A, 2nd choice = B, 3rd choice = C, 4th choice = D.
   - 2x2 grid: Top-Left = A, Top-Right = B, Bottom-Left = C, Bottom-Right = D.
4. Determine the factually correct answer to the question (ignore any wrong option the user may have already clicked).
5. Respond ONLY in valid JSON with this exact format:
{"question": "brief question text", "correct_text": "text of correct option", "letter": "A"}
If no multiple-choice question is visible on screen, return:
{"question": null, "correct_text": null, "letter": "NONE"}
"""

def find_api_key():
    """Finds API key from .env automatically by variable name or key prefix"""
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "API_KEY"):
        val = os.getenv(env_name)
        if val and val.strip():
            return env_name, val.strip().strip('"').strip("'")
    # Fallback: scan all env vars for known prefixes
    for k, v in os.environ.items():
        v_clean = v.strip().strip('"').strip("'")
        if v_clean.startswith("AIza"):
            return "GEMINI_API_KEY", v_clean
        if v_clean.startswith("gsk_"):
            return "GROQ_API_KEY", v_clean
        if v_clean.startswith("sk-"):
            return "OPENAI_API_KEY", v_clean
    return None, None

def call_gemini(api_key: str, b64_images: list) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    parts = [{"text": SYSTEM_PROMPT}]
    for b64 in b64_images:
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64}})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

def call_openai_compatible(api_key: str, b64_images: list, base_url: str, model: str) -> dict:
    content = [{"type": "text", "text": SYSTEM_PROMPT}]
    for b64 in b64_images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        return json.loads(text)

def analyze_screen_or_hovered_block() -> str:
    key_name, api_key = find_api_key()
    if not api_key:
        raise RuntimeError("No API key found in .env! Add GEMINI_API_KEY, GROQ_API_KEY, or OPENAI_API_KEY to your .env file.")

    b64_images = capture_multi_views()

    if api_key.startswith("AIza") or "GEMINI" in str(key_name) or "GOOGLE" in str(key_name):
        res = call_gemini(api_key, b64_images)
    elif api_key.startswith("gsk_") or "GROQ" in str(key_name):
        res = call_openai_compatible(
            api_key, b64_images,
            "https://api.groq.com/openai/v1/chat/completions",
            "meta-llama/llama-4-scout-17b-16e-instruct"
        )
    else:
        res = call_openai_compatible(
            api_key, b64_images,
            "https://api.openai.com/v1/chat/completions",
            "gpt-4o-mini"
        )

    q_text = res.get("question")
    c_text = res.get("correct_text")
    letter = str(res.get("letter", "NONE")).strip().upper()
    print(f"[LuxKey] Question : {q_text}", flush=True)
    print(f"[LuxKey] Correct  : {c_text} -> Option {letter}", flush=True)
    return letter


# --- 4. Shortcut Actions & Listener ---
def on_toggle():
    global luxkey_active
    luxkey_active = not luxkey_active
    if luxkey_active:
        print("[LuxKey] ON (2 fast blinks)", flush=True)
        led_toggle_on()
    else:
        print("[LuxKey] OFF (1 long blink)", flush=True)
        led_toggle_off()

def on_analyze():
    global is_scanning
    if not luxkey_active:
        print("[LuxKey] Ignored Option+W (LuxKey is OFF). Press Option+Q to turn ON.", flush=True)
        return
    if is_scanning:
        return

    def _scan_task():
        global is_scanning
        is_scanning = True
        try:
            print("\n[LuxKey] Option+W triggered -> Scanning (1 quick flash)...", flush=True)
            led_scan_started()
            time.sleep(0.5)  # Clear 0.5s pause after scan flash before answer blinks

            result = analyze_screen_or_hovered_block()

            if result in ("A", "B", "C", "D"):
                blinks = {"A": 1, "B": 2, "C": 3, "D": 4}[result]
                print(f"[LuxKey] Blinking Caps Lock {blinks} time(s) for Option {result}", flush=True)
                led_answer(result)
            else:
                print("[LuxKey] No question detected -> 5 rapid flashes", flush=True)
                led_error()
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="ignore")
            print(f"[LuxKey] API HTTP Error ({he.code}): {err_body}", flush=True)
            led_error()
        except Exception as e:
            print(f"[LuxKey] Error during scan: {e}", flush=True)
            led_error()
        finally:
            is_scanning = False

    threading.Thread(target=_scan_task, daemon=True).start()

alt_pressed = False

def on_press(key):
    global alt_pressed
    if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
        alt_pressed = True
        return
    if not alt_pressed:
        return

    vk = getattr(key, "vk", None)
    char = getattr(key, "char", None)
    if char:
        char = char.lower()

    if vk == 12 or char in ("q", "œ"):
        on_toggle()
    elif vk == 13 or char in ("w", "∑"):
        on_analyze()

def on_release(key):
    global alt_pressed
    if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
        alt_pressed = False

if __name__ == "__main__":
    set_led(0)
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        print("LuxKey running (Currently ON). Option+Q: Toggle ON/OFF | Option+W: Analyze Screen", flush=True)
        listener.join()