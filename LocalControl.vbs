' LocalControl launcher: run the monitor without a terminal and open the dashboard.
Option Explicit

Const FIRST_PORT = 8765
Const LAST_PORT = 8795
Const WAIT_SECONDS = 30

Dim fso, sh, repo, home, portFile, port, pythonw, code, i, ready
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

repo = fso.GetParentFolderName(WScript.ScriptFullName)
home = sh.ExpandEnvironmentStrings("%LOCALCONTROL_HOME%")
If InStr(home, "%LOCALCONTROL_HOME%") > 0 Then
    home = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.localcontrol"
End If
portFile = home & "\port"

' Already running? Just bring up the dashboard.
port = ReadPort(portFile)
If port > 0 Then
    If HttpStatus("http://127.0.0.1:" & port & "/api/state") = 200 Then
        OpenDashboard port
        WScript.Quit 0
    End If
End If

' 8765 is AnkiConnect's default port on many machines, so use the first free one.
port = 0
For i = FIRST_PORT To LAST_PORT
    code = HttpStatus("http://127.0.0.1:" & i & "/api/state")
    If code = 200 Then
        OpenDashboard i
        WScript.Quit 0
    ElseIf code = -1 Then
        port = i
        Exit For
    End If
Next
If port = 0 Then
    MsgBox "No free port between " & FIRST_PORT & " and " & LAST_PORT & ".", vbExclamation, "LocalControl"
    WScript.Quit 1
End If

pythonw = repo & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pythonw) Then
    MsgBox "LocalControl's Python environment is missing." & vbCrLf & vbCrLf & _
           "Run this once in the project folder:" & vbCrLf & "    uv sync", vbExclamation, "LocalControl"
    WScript.Quit 1
End If

sh.CurrentDirectory = repo
sh.Run """" & pythonw & """ -m localcontrol --port " & port & " serve --no-open", 0, False

ready = False
For i = 1 To WAIT_SECONDS * 2
    WScript.Sleep 500
    If HttpStatus("http://127.0.0.1:" & port & "/api/state") = 200 Then
        ready = True
        Exit For
    End If
Next

If Not ready Then
    MsgBox "LocalControl did not start on port " & port & "." & vbCrLf & vbCrLf & _
           "See " & home & "\server.log for details.", vbExclamation, "LocalControl"
    WScript.Quit 1
End If

OpenDashboard port

Sub OpenDashboard(p)
    sh.Run "http://127.0.0.1:" & p & "/", 1, False
End Sub

Function ReadPort(path)
    Dim f, text, n
    ReadPort = 0
    On Error Resume Next
    If fso.FileExists(path) Then
        Set f = fso.OpenTextFile(path, 1, False)
        text = Trim(f.ReadAll)
        f.Close
        If IsNumeric(text) Then
            n = CInt(text)
            If n > 0 And n < 65536 Then ReadPort = n
        End If
    End If
    Err.Clear
    On Error GoTo 0
End Function

Function HttpStatus(url)
    Dim xhr, result
    result = -1
    On Error Resume Next
    ' ServerXMLHTTP supports timeouts and ignores the browser proxy settings.
    Set xhr = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    If Err.Number <> 0 Then
        Err.Clear
        Set xhr = CreateObject("MSXML2.XMLHTTP.6.0")
    End If
    If Err.Number = 0 Then
        xhr.setTimeouts 1000, 1000, 1000, 4000
        Err.Clear
        xhr.Open "GET", url, False
        Err.Clear
        xhr.Send
        If Err.Number = 0 Then result = xhr.status
    End If
    Err.Clear
    On Error GoTo 0
    HttpStatus = result
End Function
