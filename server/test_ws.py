"""WS /ws/asr 端到端冒烟测试。

用法：
  python test_ws.py [文本] [热词1,热词2]        # TTS 合成语音测试
  python test_ws.py --file 音频文件 [热词1,...]  # 用真实音频文件测试（m4a/mp3/wav 等）
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import websockets

URI = "ws://localhost:8964/ws/asr"

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".wma"}


def parse_args():
    if len(sys.argv) > 1 and sys.argv[1] == "--file":
        audio = Path(sys.argv[2])
        hotwords = sys.argv[3].split(",") if len(sys.argv) > 3 else []
        return audio, hotwords, True
    text = sys.argv[1] if len(sys.argv) > 1 else "今天我们开会讨论一下 FastWrite 项目的进度。"
    hotwords = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    return text, hotwords, False


def decode_audio(path: Path) -> bytes:
    """任意音频 -> 16k/16bit/mono PCM（经 ffmpeg 转码）。"""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out = path.with_suffix(".16k.wav")
    subprocess.run([ffmpeg, "-y", "-i", str(path), "-ac", "1", "-ar", "16000",
                    "-sample_fmt", "s16", str(out)],
                   check=True, capture_output=True, timeout=120)
    pcm = wav_pcm(out)
    out.unlink(missing_ok=True)
    return pcm


def wav_pcm(wav_path: Path) -> bytes:
    with wave.open(str(wav_path), "rb") as w:
        raw = w.readframes(w.getnframes())
        assert w.getsampwidth() == 2 and w.getframerate() == 16000 and w.getnchannels() == 1
    return raw


def synth_wav(text: str, out: Path) -> None:
    import subprocess
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SetOutputToWaveFile('%s'); $s.Speak('%s'); $s.Dispose()" % (out, text)
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True, timeout=60)


def wav_to_pcm16k(wav_path: Path) -> bytes:
    with wave.open(str(wav_path), "rb") as w:
        raw = w.readframes(w.getnframes())
        ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
    assert sw == 2, f"expect 16-bit wav, got {sw * 8}-bit"
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        # 线性插值重采样到 16k
        n = int(len(x) * 16000 / sr)
        idx = np.arange(n) * (sr / 16000)
        x = np.interp(idx, np.arange(len(x)), x.astype(np.float64))
        x = np.clip(x, -32768, 32767).astype(np.int16)
    return x.tobytes()


async def main() -> int:
    text_or_file, HOTWORDS, is_file = parse_args()  # noqa: N816
    if is_file:
        print(f"[test] 音频文件：{text_or_file}")
        pcm = decode_audio(text_or_file)
        expect = "(真实音频，无期望文本)"
    else:
        tmp = Path(__file__).parent / "_test_tts.wav"
        print(f"[test] 合成语音：{text_or_file}")
        synth_wav(text_or_file, tmp)
        pcm = wav_to_pcm16k(tmp)
        tmp.unlink(missing_ok=True)
        expect = text_or_file
    print(f"[test] PCM {len(pcm)} bytes ({len(pcm) / 2 / 16000:.1f}s)")

    chunk = 9600 * 2  # 600ms
    got_final, got_done, final_text = False, False, ""
    async with websockets.connect(URI, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"type": "init", "hotwords": HOTWORDS}))
        print("[test] ready:", await ws.recv())

        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i:i + chunk])
            await asyncio.sleep(0.05)  # 略快于实时，验证背压无关逻辑
            try:
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.2))
                    if m["type"] == "interim":
                        print(f"[test] interim: {m['text']}")
                    elif m["type"] == "final":
                        got_final, final_text = True, m["text"]
                        print(f"[test] FINAL : {m['text']}")
            except asyncio.TimeoutError:
                pass

        await ws.send(json.dumps({"type": "stop"}))
        while True:
            m = json.loads(await ws.recv())
            if m["type"] == "final":
                got_final, final_text = True, m["text"]
                print(f"[test] FINAL : {m['text']}")
            elif m["type"] == "done":
                got_done = True
                break

    print(f"\n[test] 协议校验: final={'OK' if got_final else 'MISS'} done={'OK' if got_done else 'MISS'}")
    print(f"[test] 识别结果: {final_text!r}")
    print(f"[test] 期望文本: {expect!r}")
    return 0 if (got_final and got_done) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
