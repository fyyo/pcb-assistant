Option Explicit

' Windowless launcher for PCB Lamination Workbench (uses pythonw.exe + Edge/Chrome --app window)
Dim fso, sh, rootDir, pyw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = rootDir

pyw = rootDir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
    On Error Resume Next
    pyw = Trim(sh.Exec("cmd /c where pythonw.exe").StdOut.ReadLine())
    On Error GoTo 0
End If

If pyw = "" Or Not fso.FileExists(pyw) Then
    MsgBox "Python 3.11+ (.venv\Scripts\pythonw.exe) not found." & vbCrLf & _
           "Please run: start_workbench.bat --init-venv", _
           vbExclamation, "PCB Workbench"
    WScript.Quit 1
End If

cmd = """" & pyw & """ -X utf8 -B """ & rootDir & "\knowledge_catalog.py"" web --desktop --reuse-running --auto-port"
sh.Run cmd, 0, False
