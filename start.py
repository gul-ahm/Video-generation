import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

os.chdir(Path(__file__).parent)
pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "on"]

print("1/3 installing packages")
subprocess.run(pip + ["-r", "requirements.txt"], check=True)
if "--chatterbox" in sys.argv:
    if sys.platform == "darwin":
        # MLX build of Chatterbox: Turbo runs about 3x faster than real time on an M2, PyTorch on MPS does not.
        subprocess.run(pip + ["chatterbox-mlx==1.3.1", "accelerate"], check=True)
    else:
        cuda = ["--extra-index-url", "https://download.pytorch.org/whl/cu124"]
        subprocess.run(pip + ["torchaudio==2.6.0", "--index-url", "https://download.pytorch.org/whl/cu124"])
        subprocess.run(pip + ["chatterbox-tts==0.1.6", "accelerate"] + cuda, check=True)
        subprocess.run(pip + ["--no-deps", "chatterbox-tts==0.1.7"], check=True)
    subprocess.run([sys.executable, "-c", "import torch; print('  GPU ready:', torch.cuda.is_available() or "
                                          "torch.backends.mps.is_available())"])
esrgan = Path("tools/realesrgan")
if not esrgan.exists():
    # Real-ESRGAN (ncnn): sharp 2x upscale of every picture; the zip has builds for macOS, Windows and Linux
    name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "ubuntu")
    url = f"https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-{name}.zip"
    zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url).read())).extractall(esrgan)
    for exe in esrgan.glob("realesrgan-ncnn-vulkan*"):
        exe.chmod(0o755)
if "--kokoro" in sys.argv and subprocess.run(pip + ["kokoro>=0.9.4", "soundfile"]).returncode:
    print("  kokoro could not install on this Python, edge voices will still work")

print("2/3 testing voices")
subprocess.run([sys.executable, "voice_test.py"], check=True)

print("3/3 building a short demo video")
demo = Path("output/demo-roanoke")
demo.mkdir(parents=True, exist_ok=True)
shutil.copy("samples/roanoke.md", demo / "script.md")
subprocess.run([sys.executable, "make.py", "build", str(demo)], check=True)
print(f"\nAll good. Open {demo.resolve()} and play final.mp4")
