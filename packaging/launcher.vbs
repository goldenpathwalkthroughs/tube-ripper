' TUBE-RIPPER DELUXE 2000 - Windows launcher.
' Double-click this to start. It runs the bundled Python server with NO console
' window; the server opens the UI in your default browser at http://localhost:1337.
' Closing the browser leaves it running in the background (quit from the page's
' QUIT button, or via Task Manager: pythonw.exe). Re-running this re-opens the page.
'
' Exit code 42 from the server means "an update was installed - relaunch me",
' so the loop re-runs the freshly-swapped server automatically.

Option Explicit
Dim sh, fso, appDir, py, server, ffdir, env, code
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
py     = appDir & "\python\pythonw.exe"
server = appDir & "\app\server.py"
ffdir  = appDir & "\bin"

If Not fso.FileExists(py) Then
  MsgBox "TUBE-RIPPER is missing its Python runtime." & vbCrLf & _
         "Re-extract the whole TubeRipper-Windows folder and try again.", 16, "TUBE-RIPPER"
  WScript.Quit 1
End If

Set env = sh.Environment("PROCESS")
env("TR_APP")       = "1"
env("TR_FFMPEG_DIR") = ffdir
env("PORT")         = "1337"
env("PATH")         = ffdir & ";" & env("PATH")

Do
  ' 0 = hidden window, True = wait and return the exit code
  code = sh.Run("""" & py & """ """ & server & """ --app", 0, True)
Loop While code = 42
