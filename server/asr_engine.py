"""FunASR 流式识别引擎封装。

链路：PCM(16k/16bit/mono) --fsmn-vad 分段--> paraformer-zh-streaming 流式识别
      --> ct-punct 标点恢复 --> interim/final 文本。

每个 WebSocket 连接持有一个 Session（独立 ASR/VAD cache），互不干扰。
"""

from __future__ import annotations

import threading

import numpy as np

SAMPLE_RATE = 16000

# paraformer-zh-streaming 流式切块：600ms/块（chunk_size=[0,10,5]，60ms/帧）
ASR_CHUNK_SAMPLES = SAMPLE_RATE * 600 // 1000   # 9600
# fsmn-vad 流式切块：200ms/块
VAD_CHUNK_SAMPLES = SAMPLE_RATE * 200 // 1000   # 3200
ASR_CHUNK_SIZE = [0, 10, 5]


class ASREngine:
    """进程级单例：模型只加载一次，多连接共享（模型自身无状态，状态在 Session cache）。"""

    def __init__(self):
        from funasr import AutoModel
        # disable_update: 跳过 funasr 启动时的版本检查请求，加快启动
        self.asr = AutoModel(model="paraformer-zh-streaming",
                             vad_model=None, punc_model=None,
                             disable_update=True, disable_pbar=True)
        self.vad = AutoModel(model="fsmn-vad", disable_update=True, disable_pbar=True)
        self.punc = AutoModel(model="ct-punc", disable_update=True, disable_pbar=True)

    def punctuate(self, text: str) -> str:
        if not text:
            return text
        try:
            return self.punc.generate(input=text)[0]["text"]
        except Exception:
            return text  # 标点失败不影响主流程


_engine: ASREngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> ASREngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = ASREngine()
        return _engine


class Session:
    """单连接识别会话。

    feed(pcm_bytes) 返回待发送给客户端的消息列表：
      {"type": "interim", "text": <全量累计文本>}   实时回显
      {"type": "final",   "text": <全量累计文本>}   VAD 分段落定（含标点）
    """

    def __init__(self, engine: ASREngine, hotwords: list[str] | None = None):
        self.engine = engine
        self.hotwords = " ".join(w for w in (hotwords or []) if w.strip())
        self.reset()

    def reset(self):
        self.asr_cache: dict = {}
        self.vad_cache: dict = {}
        self.vad_buf = np.empty(0, dtype=np.int16)   # VAD 检测缓冲（只消费一份拷贝）
        self.asr_buf = np.empty(0, dtype=np.int16)   # ASR 识别缓冲（完整音频）
        self.session_text = ""                        # 已落定的全量文本（含标点）
        self.utt_parts: list[str] = []                # 当前语音段的流式识别片段

    # ---------- 内部：VAD / ASR 分块消费 ----------
    def _run_vad(self) -> bool:
        """按 200ms 块喂 VAD，检测到「语音段结束」（停顿）返回 True。"""
        endpoint = False
        while len(self.vad_buf) >= VAD_CHUNK_SAMPLES:
            chunk = self.vad_buf[:VAD_CHUNK_SAMPLES]
            self.vad_buf = self.vad_buf[VAD_CHUNK_SAMPLES:]
            try:
                res = self.engine.vad.generate(input=chunk, cache=self.vad_cache,
                                               chunk_size=200, is_final=False)
                value = res[0].get("value") if res and res[0] else None
            except Exception:
                value = None
            # value 形如 [[start_ms, -1]]（语音进行中）或 [[start_ms, end_ms]]（段结束）
            if value:
                for seg in value:
                    if len(seg) >= 2 and seg[0] >= 0 and seg[1] >= 0:
                        endpoint = True
        return endpoint

    def _run_asr(self, is_final: bool) -> None:
        """按 600ms 块喂流式 ASR；is_final 时把剩余样本全部喂入并收尾。"""
        while True:
            if is_final:
                chunk, self.asr_buf = self.asr_buf, np.empty(0, dtype=np.int16)
                if len(chunk) == 0:
                    break
            else:
                if len(self.asr_buf) < ASR_CHUNK_SAMPLES:
                    break
                chunk = self.asr_buf[:ASR_CHUNK_SAMPLES]
                self.asr_buf = self.asr_buf[ASR_CHUNK_SAMPLES:]
            try:
                # look_back 参数：官方流式示例配置，利用跨块上下文回看，减少分块边界叠字/丢字
                res = self.engine.asr.generate(input=chunk, cache=self.asr_cache,
                                               is_final=is_final, chunk_size=ASR_CHUNK_SIZE,
                                               encoder_chunk_look_back=4,
                                               decoder_chunk_look_back=1,
                                               hotword=self.hotwords or None)
                text = res[0].get("text", "") if res and res[0] else ""
            except Exception:
                text = ""
            if text:
                self.utt_parts.append(text)

    def _flush_utterance(self) -> dict | None:
        """语音段结束：收尾 ASR -> 加标点 -> 落入 session_text。"""
        self._run_asr(is_final=True)
        utt = "".join(self.utt_parts).strip()
        self.utt_parts = []
        self.asr_cache = {}  # is_final 后必须换新 cache 开启下一段
        if not utt:
            return None
        self.session_text = (self.session_text + self.engine.punctuate(utt)).strip()
        return {"type": "final", "text": self.session_text}

    # ---------- 对外接口 ----------
    def feed(self, pcm: bytes) -> list[dict]:
        """送入一段 PCM（任意长度），返回本轮产生的消息。"""
        if not pcm:
            return []
        samples = np.frombuffer(pcm, dtype=np.int16)
        self.vad_buf = np.concatenate([self.vad_buf, samples])
        self.asr_buf = np.concatenate([self.asr_buf, samples])
        messages: list[dict] = []

        endpoint = self._run_vad()
        if endpoint:
            final = self._flush_utterance()
            if final:
                messages.append(final)
        else:
            self._run_asr(is_final=False)
            if self.utt_parts:
                interim_text = self.session_text + "".join(self.utt_parts)
                messages.append({"type": "interim", "text": interim_text})
        return messages

    def finish(self) -> list[dict]:
        """录音结束：VAD/ASR 收尾，返回最后一条 final（若有）。"""
        try:
            self.engine.vad.generate(input=np.empty(0, dtype=np.int16),
                                     cache=self.vad_cache, chunk_size=200, is_final=True)
        except Exception:
            pass
        final = self._flush_utterance()
        return [final] if final else []
