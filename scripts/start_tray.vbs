Option Explicit
' Starts the AutoCheckin tray companion in the logged-on user session.
' Placed in the Startup folder by install_mumu_task.ps1 so the tray comes back on
' every logon. Launched through a VBS to keep it fully windowless.
' Keep this file ASCII-only: Windows Script Host reads .vbs using the ANSI code page.
Dim fso, shell, root, ps, script

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
script = root & "\scripts\mumu_tray.ps1"

If Not fso.FileExists(script) Then WScript.Quit 1
If IsRunning("tray-pid") Then WScript.Quit 0
' Refuse to start a tray whose menu would have no labels: a broken wording resource once
' produced exactly that, and an unusable tray is worse than none because it looks alive.
If Not TextOk() Then WScript.Quit 2

ps = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
If Not fso.FileExists(ps) Then ps = "powershell.exe"

shell.Run """" & ps & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & """", 0, False

' Reads the helper's single-line answer. VBScript's Trim() leaves CR/LF in place, so
' newlines are stripped explicitly: comparing "ok" & vbCrLf against "ok" is False, which
' silently turned this gate into "always fail" and blocked the tray from starting.
Function HelperLine(what)
  Dim python, helper, temp, value
  HelperLine = ""
  python = root & "\.venv\Scripts\python.exe"
  helper = root & "\scripts\tray_helper.py"
  If Not fso.FileExists(python) Then Exit Function
  If Not fso.FileExists(helper) Then Exit Function
  temp = shell.ExpandEnvironmentStrings("%TEMP%") & "\autocheckin-" & what & ".txt"
  On Error Resume Next
  If fso.FileExists(temp) Then fso.DeleteFile temp, True
  On Error GoTo 0
  shell.Run "cmd /c """"" & python & """ """ & helper & """ " & what & " > """ & temp & """ 2>nul""", 0, True
  If fso.FileExists(temp) Then
    On Error Resume Next
    value = fso.OpenTextFile(temp, 1).ReadAll
    On Error GoTo 0
    value = Replace(value, vbCr, "")
    value = Replace(value, vbLf, "")
    HelperLine = Trim(value)
  End If
End Function

Function IsRunning(what)
  Dim pid
  pid = HelperLine(what)
  IsRunning = (IsNumeric(pid) And CLng(pid) > 0)
End Function

Function TextOk()
  TextOk = (HelperLine("text-check") = "ok")
End Function
