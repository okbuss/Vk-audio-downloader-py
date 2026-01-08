import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import ctypes


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN0PQRSTUVWXYZO123456789+/="


def ensure_dependencies():
    print("Checking dependencies...")
    missing = []
    if importlib.util.find_spec("yt_dlp") is None:
        missing.append("yt-dlp")
    if importlib.util.find_spec("Cryptodome") is None:
        missing.append("pycryptodomex")

    if missing:
        print("Downloading dependencies...")
        cmd = [sys.executable, "-m", "pip", "install"] + missing
        subprocess.check_call(cmd)
    else:
        print("All good.")

    if not ensure_ffmpeg():
        print("ffmpeg is missing; MP3 conversion will be skipped.")


def run_command(cmd):
    try:
        subprocess.check_call(cmd)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ensure_ffmpeg():
    if shutil.which("ffmpeg"):
        return True

    print("ffmpeg not found. Attempting to install...")
    if os.name == "nt":
        installers = [
            [
                "winget",
                "install",
                "--id",
                "Gyan.FFmpeg",
                "-e",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            ["choco", "install", "ffmpeg", "-y"],
            ["scoop", "install", "ffmpeg"],
        ]
        for cmd in installers:
            if run_command(cmd):
                break
    elif sys.platform == "darwin":
        if shutil.which("brew"):
            run_command(["brew", "install", "ffmpeg"])
    else:
        print("Install ffmpeg with your system package manager.")

    return shutil.which("ffmpeg") is not None


def parse_args():
    parser = argparse.ArgumentParser(description="Download VK audio to MP3.")
    parser.add_argument(
        "--secure",
        action="store_true",
        help="Verify TLS certificates (no insecure bypass).",
    )
    return parser.parse_args()


def get_ssl_context(secure):
    if secure:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def read_vk_cookies_from_db(db_path):
    if not os.path.isfile(db_path):
        raise RuntimeError("cookies.sqlite not found at the provided path.")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(tmp_fd)
    shutil.copy2(db_path, tmp_path)

    try:
        conn = sqlite3.connect(tmp_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT name, value, host, path, expiry, isSecure "
            "FROM moz_cookies "
            "WHERE host LIKE '%vk.com%' OR host LIKE '%vkuseraudio.net%' "
            "OR host LIKE '%useraudio.net%'"
        )
        rows = cur.fetchall()
        conn.close()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not rows:
        raise RuntimeError("No VK cookies found in the provided cookies.sqlite.")

    cookie_header = "; ".join([f"{name}={value}" for name, value, *_ in rows])
    return cookie_header, rows


def get_firefox_cookies():
    base = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox", "Profiles")
    if not os.path.isdir(base):
        raise RuntimeError("Firefox profile directory not found.")

    profiles = glob.glob(os.path.join(base, "*"))
    if not profiles:
        raise RuntimeError("No Firefox profiles found.")

    ordered = []
    for p in profiles:
        if p.endswith(".default-release"):
            ordered.append(p)
    ordered.extend([p for p in profiles if p not in ordered])

    last_error = None
    for profile in ordered:
        db_path = os.path.join(profile, "cookies.sqlite")
        if not os.path.isfile(db_path):
            continue
        try:
            cookie_header, rows = read_vk_cookies_from_db(db_path)
            return cookie_header, rows, db_path
        except Exception as exc:
            last_error = exc

    if last_error:
        raise RuntimeError(str(last_error))
    raise RuntimeError("No cookies.sqlite files found in Firefox profiles.")


def get_config_path():
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "vk_audio_downloader", "config.json")


def load_config():
    path = get_config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    path = get_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def write_netscape_cookies(rows):
    fd, path = tempfile.mkstemp(prefix="vk_cookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for name, value, host, cpath, expiry, is_secure in rows:
            if not host or not name:
                continue
            if host.endswith("vkuseraudio.net"):
                domain = ".vkuseraudio.net"
                include_sub = "TRUE"
            elif host.endswith("useraudio.net"):
                domain = ".useraudio.net"
                include_sub = "TRUE"
            elif host.endswith("vk.com"):
                domain = ".vk.com"
                include_sub = "TRUE"
            else:
                domain = host
                include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path_val = cpath or "/"
            secure = "TRUE" if is_secure else "FALSE"
            exp = int(expiry) if expiry else 0
            f.write(
                f"{domain}\t{include_sub}\t{path_val}\t{secure}\t"
                f"{exp}\t{name}\t{value}\n"
            )
    return path


def get_downloads_dir():
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            fid = GUID(
                0x374DE290,
                0x123F,
                0x4565,
                (ctypes.c_ubyte * 8)(
                    0x91,
                    0x64,
                    0x39,
                    0xC4,
                    0x92,
                    0x5E,
                    0x46,
                    0x7B,
                ),
            )
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            shell32.SHGetKnownFolderPath.restype = wintypes.HRESULT

            path_ptr = ctypes.c_wchar_p()
            result = shell32.SHGetKnownFolderPath(
                ctypes.byref(fid), 0, None, ctypes.byref(path_ptr)
            )
            if result == 0 and path_ptr.value:
                path = path_ptr.value
                ctypes.windll.ole32.CoTaskMemFree(
                    ctypes.cast(path_ptr, ctypes.c_void_p)
                )
                return path
        except Exception:
            pass

        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            for name in [
                "{374DE290-123F-4565-9164-39C4925E467B}",
                "Downloads",
            ]:
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    if val:
                        return os.path.expandvars(val)
                except OSError:
                    continue
        except Exception:
            pass

        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return os.path.join(userprofile, "Downloads")

    home = os.path.expanduser("~")
    xdg = os.path.join(home, ".config", "user-dirs.dirs")
    if os.path.isfile(xdg):
        try:
            with open(xdg, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("XDG_DOWNLOAD_DIR"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            path = parts[1].strip().strip('"')
                            path = path.replace("$HOME", home)
                            return os.path.expandvars(path)
        except Exception:
            pass

    return os.path.join(home, "Downloads")


def parse_audio_link(link):
    match = re.search(r"audio(-?\d+)_(\d+)(?:_([A-Za-z0-9]+))?", link)
    if not match:
        raise ValueError("Could not parse VK audio link.")

    owner_id, audio_id, access_key = match.groups()
    token = f"{owner_id}_{audio_id}"
    if access_key:
        token = f"{token}_{access_key}"
    return token


def fetch_audio_tuple(audio_token, cookie_header, ssl_context):
    params = {"act": "reload_audios", "audio_ids": audio_token, "al": "1"}
    url = "https://vk.com/al_audio.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://vk.com/audio",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie_header,
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=ssl_context) as resp:
        data = resp.read().decode("utf-8", errors="replace")

    obj = json.loads(data)
    payload = obj.get("payload")
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError("Unexpected VK response payload.")

    status = payload[0]
    if status not in (0, "0"):
        raise RuntimeError(f"VK returned status {status}.")

    try:
        audio_tuple = payload[1][0][0]
    except Exception as exc:
        raise RuntimeError("Could not locate audio tuple in payload.") from exc

    return audio_tuple


def find_vk_id(audio_tuple):
    for item in audio_tuple:
        if isinstance(item, dict) and "vk_id" in item:
            return item.get("vk_id")
    return None


def b64_decode(s):
    if not s or len(s) % 4 == 1:
        return None
    t = 0
    r = 0
    out = []
    for ch in s:
        n = ALPHABET.find(ch)
        if n == -1:
            continue
        if r % 4:
            t = 64 * t + n
        else:
            t = n
        r_mod = r % 4
        r += 1
        if r_mod:
            out.append(chr(255 & (t >> (-2 * r & 6))))
    return "".join(out)


def c_v(e):
    return e[::-1]


def c_r(e, t):
    t = int(t)
    r = ALPHABET + ALPHABET
    e_list = list(e)
    for i in range(len(e_list) - 1, -1, -1):
        n = r.find(e_list[i])
        if n != -1:
            pos = n - t
            if pos < 0:
                pos += len(r)
            e_list[i] = r[pos]
    return "".join(e_list)


def permute_indices_int(length, seed):
    r = [0] * length
    if length:
        t = abs(int(seed))
        for i in range(length - 1, -1, -1):
            t = (length * (i + 1) ^ t + i) % length
            r[i] = t
    return r


def permute_indices_big(length, seed):
    r = [0] * length
    if length == 0:
        return r
    seed = int(seed)
    if seed < 0:
        seed = -seed
    m = length
    for o in range(length - 1, -1, -1):
        seed = (m * (o + 1) ^ seed + o) % m
        r[o] = seed
    return r


def c_s(e, seed, use_big=False):
    n = len(e)
    if n == 0:
        return e
    r = permute_indices_big(n, seed) if use_big else permute_indices_int(n, seed)
    e_list = list(e)
    o = 0
    while True:
        o += 1
        if o >= n:
            break
        idx = r[n - 1 - o]
        current = e_list[o]
        removed = e_list.pop(idx)
        e_list.insert(idx, current)
        e_list[o] = removed
    return "".join(e_list)


def c_i(e, t, vk_id):
    try:
        n = int(t, 10)
    except Exception:
        n = 0
    r = (vk_id or 0) ^ n
    return c_s(e, r, use_big=True)


def c_x(e, t):
    if not t:
        return e
    k = ord(t[0])
    return "".join([chr(ord(ch) ^ k) for ch in e])


def decode_audio_url(url, vk_id):
    if "audio_api_unavailable" not in url:
        return url
    try:
        extra = url.split("?extra=")[1]
    except Exception:
        return url
    parts = extra.split("#")
    if len(parts) < 2:
        return url
    r = b64_decode(parts[0])
    o = "" if parts[1] == "" else b64_decode(parts[1])
    if not isinstance(o, str) or not r:
        return url
    ops = o.split(chr(9)) if o else []
    for op in reversed(ops):
        n = op.split(chr(11))
        t = n.pop(0) if n else ""
        n.insert(0, r)
        if t == "v":
            r = c_v(*n)
        elif t == "r":
            r = c_r(*n)
        elif t == "s":
            r = c_s(*n)
        elif t == "i":
            r = c_i(n[0], n[1] if len(n) > 1 else "0", vk_id)
        elif t == "x":
            r = c_x(*n)
        else:
            return url
    if r.startswith("http"):
        return r
    return url


def sanitize_filename(name):
    safe = re.sub(r"[^A-Za-z0-9 ._\\-]+", "_", name)
    safe = safe.strip()
    return safe if safe else "vk_audio"


def choose_base(download_dir, base):
    candidate = base
    idx = 1
    while glob.glob(os.path.join(download_dir, candidate + ".*")):
        candidate = f"{base}_{idx}"
        idx += 1
    return candidate


def run_yt_dlp(url, output_template, cookies_path, secure):
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        url,
        "-o",
        output_template,
        "--user-agent",
        UA,
        "--add-header",
        "Referer: https://vk.com/",
        "--cookies",
        cookies_path,
        "--hls-prefer-native",
        "--no-mtime",
    ]
    if not secure:
        cmd.append("--no-check-certificate")
    subprocess.check_call(cmd)


def find_downloaded_file(download_dir, base):
    candidates = glob.glob(os.path.join(download_dir, base + ".*"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def convert_to_mp3(input_path, cleanup_original=True):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found; leaving original file as-is.")
        print("Install ffmpeg to enable MP3 conversion.")
        return input_path

    ext = os.path.splitext(input_path)[1].lower()
    if ext == ".mp3":
        return input_path

    base = os.path.splitext(input_path)[0]
    mp3_path = base + ".mp3"
    cmd_mp3 = [ffmpeg, "-y", "-i", input_path, "-vn", "-q:a", "2", mp3_path]
    result = subprocess.run(cmd_mp3)
    if result.returncode == 0 and os.path.isfile(mp3_path):
        if cleanup_original and os.path.isfile(input_path):
            try:
                os.remove(input_path)
            except OSError:
                pass
        return mp3_path

    print("Audio conversion failed; keeping original file.")
    if os.path.isfile(mp3_path):
        try:
            os.remove(mp3_path)
        except OSError:
            pass
    return input_path


def main():
    args = parse_args()
    try:
        ensure_dependencies()
    except Exception as exc:
        print(f"Dependency setup failed: {exc}")
        sys.exit(1)

    print("Enter vk audio link")
    link = input("> ").strip()
    if not link:
        print("No link provided.")
        sys.exit(1)

    try:
        audio_token = parse_audio_link(link)
    except Exception as exc:
        print(f"Link error: {exc}")
        sys.exit(1)

    cookie_header = None
    cookie_rows = None
    cookies_db_path = None

    try:
        print("Checking Firefox cookies...")
        cookie_header, cookie_rows, cookies_db_path = get_firefox_cookies()
    except Exception as exc:
        cfg = load_config()
        saved_path = cfg.get("cookies_db_path")
        if saved_path:
            try:
                print("Trying saved cookies path...")
                cookie_header, cookie_rows = read_vk_cookies_from_db(saved_path)
                cookies_db_path = saved_path
            except Exception as exc2:
                print(f"Saved cookie path failed: {exc2}")

        if cookie_header is None:
            print(f"Cookie error: {exc}")
            print("Typical Firefox cookie locations:")
            print("  Windows: %APPDATA%\\Mozilla\\Firefox\\Profiles\\<profile>\\cookies.sqlite")
            print("  Linux:   ~/.mozilla/firefox/<profile>/cookies.sqlite")
            print("  macOS:   ~/Library/Application Support/Firefox/Profiles/<profile>/cookies.sqlite")
            print("Enter full path to cookies.sqlite (leave blank to abort)")
            manual_path = input("> ").strip().strip("\"'")
            if not manual_path:
                sys.exit(1)
            try:
                cookie_header, cookie_rows = read_vk_cookies_from_db(manual_path)
                cookies_db_path = manual_path
                cfg = load_config()
                cfg["cookies_db_path"] = manual_path
                save_config(cfg)
            except Exception as exc2:
                print(f"Cookie error: {exc2}")
                sys.exit(1)

    ssl_context = get_ssl_context(args.secure)

    try:
        audio_tuple = fetch_audio_tuple(audio_token, cookie_header, ssl_context)
    except Exception as exc:
        print(f"VK fetch error: {exc}")
        sys.exit(1)

    if len(audio_tuple) < 5:
        print("Unexpected audio tuple format.")
        sys.exit(1)

    audio_url = audio_tuple[2]
    title = str(audio_tuple[3]) if audio_tuple[3] else "vk_audio"
    artist = str(audio_tuple[4]) if audio_tuple[4] else ""
    name = f"{artist} - {title}".strip(" -")
    base_name = sanitize_filename(name)

    vk_id = find_vk_id(audio_tuple)
    if vk_id is None:
        print("Warning: VK user id not found; decode may fail.")

    decoded_url = decode_audio_url(audio_url, vk_id)
    if "audio_api_unavailable" in decoded_url:
        print("Failed to decode audio URL.")
        sys.exit(1)

    download_dir = get_downloads_dir()
    if not download_dir:
        print("Could not resolve Downloads directory.")
        sys.exit(1)
    os.makedirs(download_dir, exist_ok=True)

    base = choose_base(download_dir, base_name)
    output_template = os.path.join(download_dir, base + ".%(ext)s")

    cookies_path = None
    try:
        cookies_path = write_netscape_cookies(cookie_rows)
    except Exception as exc:
        print(f"Cookie export error: {exc}")
        sys.exit(1)

    print("Downloading...")
    try:
        run_yt_dlp(decoded_url, output_template, cookies_path, args.secure)
    except Exception as exc:
        print(f"Download failed: {exc}")
        sys.exit(1)
    finally:
        if cookies_path and os.path.isfile(cookies_path):
            try:
                os.remove(cookies_path)
            except OSError:
                pass

    downloaded = find_downloaded_file(download_dir, base)
    if not downloaded:
        print("Could not locate downloaded file.")
        sys.exit(1)

    final_path = convert_to_mp3(downloaded)
    print(f"Saved: {final_path}")


if __name__ == "__main__":
    main()
