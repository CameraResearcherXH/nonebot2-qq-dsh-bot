' start_comfyui.vbs - hidden launch ComfyUI (draw service, 127.0.0.1:8188)
' Do NOT use activate.bat (blocked by ExecutionPolicy); call venv python directly.
Set WshShell = CreateObject("WScript.Shell")

' working dir must be ComfyUI root (main.py resolves models/ relative to cwd)
WshShell.CurrentDirectory = "H:\ComfyUI"

' 0 = hidden window, False = don't wait
WshShell.Run """H:\ComfyUI\venv\Scripts\python.exe"" main.py --listen 127.0.0.1 --port 8188", 0, False
