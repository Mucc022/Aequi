from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


class WhisperTranscriber:
    def __init__(
        self,
        model_size: str = "medium",
        language: str = "zh",
        device: str = "auto",
        compute_type: str | None = None,
    ) -> None:
        self._prepare_windows_cuda_runtime()
        whisper_model_cls = self._get_whisper_model_cls()
        self._whisper_model_cls = whisper_model_cls
        self.model_size = model_size
        chosen_device = self._choose_device(device)
        if compute_type is None:
            compute_type = "float16" if chosen_device == "cuda" else "int8"

        self.language = None if language.lower() == "auto" else language
        self.device = chosen_device
        self.compute_type = compute_type

        logger.info(
            "Loading model '%s' on device '%s' (compute_type=%s).",
            model_size,
            self.device,
            self.compute_type,
        )
        try:
            self.model = self._load_model()
        except Exception as exc:
            if self.device != "cuda" or not self._is_cuda_runtime_error(exc):
                raise
            self._fallback_to_cpu()

    def _load_model(self):
        return self._whisper_model_cls(
            model_size_or_path=self.model_size,
            device=self.device,
            compute_type=self.compute_type,
        )

    def _fallback_to_cpu(self) -> None:
        logger.warning("检测到 CUDA 依赖缺失，已自动回退到 CPU。")
        self.device = "cpu"
        self.compute_type = "int8"
        self.model = self._load_model()

    @staticmethod
    def _is_cuda_runtime_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        needles = (
            "cuda",
            "cublas",
            "cudnn",
            "cufft",
            "curand",
            "cusolver",
            "cusparse",
            "cudart",
            "dll",
            "library",
        )
        return any(needle in text for needle in needles)

    @staticmethod
    def _prepare_windows_cuda_runtime() -> None:
        if os.name != "nt":
            return

        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        candidate_dirs = [
            site_packages / "nvidia" / "cublas" / "bin",
            site_packages / "nvidia" / "cudnn" / "bin",
            site_packages / "nvidia" / "cuda_nvrtc" / "bin",
        ]
        dll_dirs = [p for p in candidate_dirs if p.is_dir()]
        if not dll_dirs:
            return

        path_items = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        for dll_dir in dll_dirs:
            dll_dir_str = str(dll_dir)
            if dll_dir_str not in path_items:
                path_items.insert(0, dll_dir_str)
            try:
                os.add_dll_directory(dll_dir_str)
            except (AttributeError, FileNotFoundError, OSError):
                pass
        os.environ["PATH"] = os.pathsep.join(path_items)

    @staticmethod
    def _get_whisper_model_cls():
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency 'faster-whisper'. Run: pip install -r requirements.txt"
            ) from exc
        return WhisperModel

    @staticmethod
    def _choose_device(device: str) -> str:
        if device in {"cpu", "cuda"}:
            return device
        if device != "auto":
            return "cpu"

        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            pass

        return "cpu"

    def transcribe_to_files(self, audio_path: Path, txt_output_path: Path, srt_output_path: Path) -> int:
        txt_output_path.parent.mkdir(parents=True, exist_ok=True)
        srt_output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            segment_count = self._transcribe_once(audio_path, txt_output_path, srt_output_path)
        except Exception as exc:
            if self.device != "cuda" or not self._is_cuda_runtime_error(exc):
                raise
            self._fallback_to_cpu()
            segment_count = self._transcribe_once(audio_path, txt_output_path, srt_output_path)

        if segment_count == 0:
            raise RuntimeError(
                "No speech segments recognized. Try rerun with --language auto, "
                "or check if the media has audible speech."
            )

        return segment_count

    def _transcribe_once(self, audio_path: Path, txt_output_path: Path, srt_output_path: Path) -> int:
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=5,
            vad_filter=True,
        )

        logger.info("Detected language: %s (probability: %.2f)", info.language, info.language_probability)

        segment_count = 0
        with txt_output_path.open("w", encoding="utf-8") as txt_file, srt_output_path.open(
            "w", encoding="utf-8"
        ) as srt_file:
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                segment_count += 1
                txt_file.write(text + "\n")
                srt_file.write(f"{segment_count}\n")
                srt_file.write(
                    f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}\n"
                )
                srt_file.write(text + "\n\n")
        return segment_count
