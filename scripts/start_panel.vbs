Option Explicit
' Console shortcut: make sure the background service and its tray companion are
' running, then open the dashboard. Safe to double-click at any time.
' NOTE: keep this file ASCII-only. Windows Script Host reads .vbs using the ANSI
' code page, so non-ASCII text here would be mangled.
Dim fso, root, shell, pythonw, serviceLock, i

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonw = root & "\.venv\Scripts\pythonw.exe"
serviceLock = root & "\data\service.pid"

If Not fso.FileExists(pythonw) Then
  MsgBox "Local Python environment not found:" & vbCrLf & pythonw & vbCrLf & vbCrLf & _
         "Reinstall it as described in README.md.", 16, "AutoCheckin"
  WScript.Quit 1
End If

' Re-opening the console is an explicit "start it" request, so an earlier manual stop
' is released here. A stop therefore lasts until the next reboot or until you open the
' console again; the tray's "stop" item keeps the service down in the meantime.
RunHelper "clear-stop"
RunHelper "guard"

For i = 1 To 30
  If ServicePid() > 0 Then Exit For
  WScript.Sleep 500
Next

If ServicePid() = 0 Then
  If MsgBox("The background service did not start." & vbCrLf & vbCrLf & _
            "This is expected if you stopped it on purpose (it returns after a" & vbCrLf & _
            "reboot), or it can mean the local environment is broken." & vbCrLf & vbCrLf & _
            "Open the dashboard page anyway?", 52, "AutoCheckin") = 7 Then
    WScript.Quit 1
  End If
End If

shell.Run "http://127.0.0.1:18765/", 1, False

' Ask the Python helper instead of parsing tasklist: "tasklist | find <pid>" also
' matches a pid appearing as a substring, which reported dead processes as alive.
Function ServicePid()
  Dim python, helper, temp, pid
  ServicePid = 0
  python = root & "\.venv\Scripts\python.exe"
  helper = root & "\scripts\tray_helper.py"
  If Not fso.FileExists(python) Then Exit Function
  If Not fso.FileExists(helper) Then Exit Function
  temp = shell.ExpandEnvironmentStrings("%TEMP%") & "\autocheckin-service-pid.txt"
  shell.Run "cmd /c """"" & python & """ """ & helper & """ service-pid > """ & temp & """ 2>nul""", 0, True
  If fso.FileExists(temp) Then
    On Error Resume Next
    pid = Trim(fso.OpenTextFile(temp, 1).ReadAll)
    On Error GoTo 0
    If IsNumeric(pid) Then ServicePid = CLng(pid)
  End If
End Function

Sub RunHelper(what)
  Dim python, helper
  python = root & "\.venv\Scripts\python.exe"
  helper = root & "\scripts\tray_helper.py"
  If Not fso.FileExists(python) Then Exit Sub
  shell.Run "cmd /c """"" & python & """ """ & helper & """ " & what & """", 0, False
End Sub
