from pathlib import Path
from typing import Dict, Optional, Union
import gc
import whisperx
import torch

from .base_transcriber import BaseTranscriber


class WhisperTranscriber(BaseTranscriber):
    """Speech-to-text transcriber using WhisperX."""

    def __init__(
        self,
        model_name: str = "base",
        device: Optional[str] = None,
        compute_type: str = "float16",
        model_store_dir: Optional[Path] = None,
    ):
        """Initialize the WhisperX transcriber.

        Args:
            model_name: WhisperX model name (tiny, base, small, medium, large-v2, large-v3)
            device: Device to run on ("cpu", "cuda"). Auto-detect if None.
            compute_type: Compute precision ("float16", "int8", "float32")
            model_store_dir: Directory to store downloaded models (default: ./models)
        """
        super().__init__(device=device, model_store_dir=model_store_dir)
        self.model_name = model_name
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.compute_type = compute_type

        import os

        self.batch_size = int(os.getenv("WHISPERX_BATCH_SIZE", "16"))
        self.compute_type = os.getenv("WHISPERX_COMPUTE_TYPE", compute_type)

        self.whisper_cache_dir = self.model_store_dir / "whisperx"
        self.whisper_cache_dir.mkdir(parents=True, exist_ok=True)

        self.alignment_cache_dir = self.model_store_dir / "alignment"
        self.alignment_cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_alignment_cache_dir(self, language: str) -> str:
        """Get alignment cache directory for specific language, creating if needed."""
        lang_cache_dir = self.alignment_cache_dir / language
        lang_cache_dir.mkdir(parents=True, exist_ok=True)
        return str(lang_cache_dir)

    def transcribe_audio(
        self,
        audio_path: Union[str, Path],
        language: Optional[str] = None,
        diarize: bool = False,
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
    ) -> Dict:
        """Transcribe audio file to text.

        Args:
            audio_path: Path to audio file
            language: Language code (e.g., "zh", "en"). Auto-detect if None.
            diarize: Whether to perform speaker diarization
            num_speakers: Number of speakers (if known)
            min_speakers: Minimum number of speakers
            max_speakers: Maximum number of speakers

        Returns:
            Dictionary with transcription results including segments and word timings

        Raises:
            Exception: If transcription fails
        """
        try:
            model = whisperx.load_model(
                self.model_name,
                self.device,
                compute_type=self.compute_type,
                download_root=str(self.whisper_cache_dir),
            )

            audio = whisperx.load_audio(str(audio_path))
            result = model.transcribe(
                audio, batch_size=self.batch_size, language=language
            )

            align_model, metadata = whisperx.load_align_model(
                language_code=result["language"],
                device=self.device,
                model_dir=self._get_alignment_cache_dir(result["language"]),
            )

            # Store language before alignment
            detected_language = result["language"]

            # Align whisper output
            result = whisperx.align(
                result["segments"],
                align_model,
                metadata,
                audio,
                self.device,
                return_char_alignments=False,
            )

            # Preserve language information
            result["language"] = detected_language

            # Perform speaker diarization if requested
            if diarize:
                import os

                hf_token = os.getenv("HF_TOKEN")
                if hf_token:
                    diarize_model = whisperx.diarize.DiarizationPipeline(
                        use_auth_token=hf_token, device=self.device
                    )

                    # Build parameters for diarization
                    diarize_params = {}
                    if num_speakers is not None:
                        diarize_params["num_speakers"] = num_speakers
                    if min_speakers is not None:
                        diarize_params["min_speakers"] = min_speakers
                    if max_speakers is not None:
                        diarize_params["max_speakers"] = max_speakers

                    diarize_segments = diarize_model(audio, **diarize_params)
                    result = whisperx.assign_word_speakers(
                        diarize_segments, result, fill_nearest=False
                    )
                else:
                    raise Exception(
                        "HF_TOKEN environment variable required for speaker diarization"
                    )

            return result

        except Exception as e:
            raise Exception(f"Failed to transcribe audio {audio_path}: {str(e)}")
        finally:
            # Clean up GPU memory to prevent VRAM leaks
            if 'model' in locals():
                del model
            if 'align_model' in locals():
                del align_model
            if 'diarize_model' in locals():
                del diarize_model
            
            # Force garbage collection and clear CUDA cache
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def format_transcript(self, result: Dict, format_type: str = "text") -> str:
        """Format transcription result.

        Args:
            result: Transcription result from transcribe_audio
            format_type: Output format ("text", "srt", "vtt")

        Returns:
            Formatted transcript string
        """
        if format_type == "text":
            return self._format_text(result)
        elif format_type == "srt":
            return self._format_srt(result)
        elif format_type == "vtt":
            return self._format_vtt(result)
        else:
            raise ValueError(f"Unsupported format: {format_type}")

    def _format_text(self, result: Dict) -> str:
        """Format as plain text."""
        segments = result.get("segments", [])
        lines = []

        for segment in segments:
            words = segment.get("words", [])
            if words and any("speaker" in word for word in words):
                # Group words by speaker
                current_speaker = None
                current_text = []

                for word in words:
                    word_speaker = word.get("speaker")
                    if word_speaker != current_speaker:
                        # Speaker changed, output previous group
                        if current_speaker and current_text:
                            lines.append(
                                f"[{current_speaker}] {' '.join(current_text).strip()}"
                            )
                        current_speaker = word_speaker
                        current_text = []

                    if "word" in word:
                        current_text.append(word["word"])

                # Output final group
                if current_speaker and current_text:
                    lines.append(
                        f"[{current_speaker}] {' '.join(current_text).strip()}"
                    )
            else:
                # Fallback to segment-level speaker or no speaker
                text = segment["text"].strip()
                if "speaker" in segment:
                    lines.append(f"[{segment['speaker']}] {text}")
                else:
                    lines.append(text)

        return "\n".join(lines)

    def _format_srt(self, result: Dict) -> str:
        """Format as SRT subtitle file."""
        segments = result.get("segments", [])
        srt_content = []
        subtitle_index = 1

        for segment in segments:
            words = segment.get("words", [])
            if words and any("speaker" in word for word in words):
                # Group words by speaker within the segment
                current_speaker = None
                current_text = []
                current_start = segment["start"]

                for i, word in enumerate(words):
                    word_speaker = word.get("speaker")
                    if word_speaker != current_speaker:
                        # Speaker changed, create subtitle for previous group
                        if current_speaker and current_text:
                            word_end = words[i - 1].get("end", current_start + 1)
                            start_time = self._seconds_to_srt_time(current_start)
                            end_time = self._seconds_to_srt_time(word_end)
                            text = (
                                f"[{current_speaker}] {' '.join(current_text).strip()}"
                            )

                            srt_content.append(f"{subtitle_index}")
                            srt_content.append(f"{start_time} --> {end_time}")
                            srt_content.append(text)
                            srt_content.append("")
                            subtitle_index += 1

                        current_speaker = word_speaker
                        current_text = []
                        current_start = word.get("start", current_start)

                    if "word" in word:
                        current_text.append(word["word"])

                # Output final group in this segment
                if current_speaker and current_text:
                    end_time = self._seconds_to_srt_time(segment["end"])
                    start_time = self._seconds_to_srt_time(current_start)
                    text = f"[{current_speaker}] {' '.join(current_text).strip()}"

                    srt_content.append(f"{subtitle_index}")
                    srt_content.append(f"{start_time} --> {end_time}")
                    srt_content.append(text)
                    srt_content.append("")
                    subtitle_index += 1
            else:
                # Fallback to segment-level
                start_time = self._seconds_to_srt_time(segment["start"])
                end_time = self._seconds_to_srt_time(segment["end"])
                text = segment["text"].strip()

                if "speaker" in segment:
                    text = f"[{segment['speaker']}] {text}"

                srt_content.append(f"{subtitle_index}")
                srt_content.append(f"{start_time} --> {end_time}")
                srt_content.append(text)
                srt_content.append("")
                subtitle_index += 1

        return "\n".join(srt_content)

    def _format_vtt(self, result: Dict) -> str:
        """Format as WebVTT file."""
        segments = result.get("segments", [])
        vtt_content = ["WEBVTT", ""]

        for segment in segments:
            words = segment.get("words", [])
            if words and any("speaker" in word for word in words):
                # Group words by speaker within the segment
                current_speaker = None
                current_text = []
                current_start = segment["start"]

                for i, word in enumerate(words):
                    word_speaker = word.get("speaker")
                    if word_speaker != current_speaker:
                        # Speaker changed, create subtitle for previous group
                        if current_speaker and current_text:
                            word_end = words[i - 1].get("end", current_start + 1)
                            start_time = self._seconds_to_vtt_time(current_start)
                            end_time = self._seconds_to_vtt_time(word_end)
                            text = (
                                f"[{current_speaker}] {' '.join(current_text).strip()}"
                            )

                            vtt_content.append(f"{start_time} --> {end_time}")
                            vtt_content.append(text)
                            vtt_content.append("")

                        current_speaker = word_speaker
                        current_text = []
                        current_start = word.get("start", current_start)

                    if "word" in word:
                        current_text.append(word["word"])

                # Output final group in this segment
                if current_speaker and current_text:
                    end_time = self._seconds_to_vtt_time(segment["end"])
                    start_time = self._seconds_to_vtt_time(current_start)
                    text = f"[{current_speaker}] {' '.join(current_text).strip()}"

                    vtt_content.append(f"{start_time} --> {end_time}")
                    vtt_content.append(text)
                    vtt_content.append("")
            else:
                # Fallback to segment-level
                start_time = self._seconds_to_vtt_time(segment["start"])
                end_time = self._seconds_to_vtt_time(segment["end"])
                text = segment["text"].strip()

                if "speaker" in segment:
                    text = f"[{segment['speaker']}] {text}"

                vtt_content.append(f"{start_time} --> {end_time}")
                vtt_content.append(text)
                vtt_content.append("")

        return "\n".join(vtt_content)
