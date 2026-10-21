Option Explicit

Dim fso, shell, root, environment, executable, pathResult
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)

Function CheckRuntimePath(candidate)
    Dim index, character, cudaLibrary
    CheckRuntimePath = 0
    For index = 1 To Len(candidate)
        character = AscW(Mid(candidate, index, 1))
        If character < 0 Or character > 127 Then
            CheckRuntimePath = 2
            Exit Function
        End If
    Next
    cudaLibrary = fso.BuildPath(candidate, "DUCAI_STUDIO_DATA\paddle_runtime\.venv\Lib\site-packages\paddle\..\nvidia\cudnn\bin\cudnn_engines_precompiled64_9.dll")
    If Len(cudaLibrary) > 240 Then CheckRuntimePath = 3
End Function

' Validate without launching anything; used by the Windows packaging tests.
If WScript.Arguments.Count = 2 Then
    If WScript.Arguments(0) = "--check-path" Then
        pathResult = CheckRuntimePath(WScript.Arguments(1))
        WScript.Echo "DUCAI_PATH_CHECK=" & pathResult
        WScript.Quit pathResult
    End If
End If

pathResult = CheckRuntimePath(root)
If pathResult <> 0 Then
    MsgBox "Paddle cannot load models from accented/non-Latin paths or CUDA DLLs from very long paths." & vbCrLf & "Move this entire test folder to E:\DUCAI-RTX50 (or C:\DUCAI-RTX50), then open this launcher again. Spaces are allowed. No driver changes are needed for this path error.", 16, "DUCAI RTX50 OCR TEST"
    WScript.Quit pathResult
End If

executable = fso.BuildPath(root, "DucaiStudio2.exe")
If Not fso.FileExists(executable) Then
    MsgBox "Extract the entire test package before launching.", 16, "DUCAI RTX50 OCR TEST"
    WScript.Quit 1
End If

' Process-local settings only: no registry writes, driver install or console.
Set environment = shell.Environment("Process")
environment("DUCAI_STUDIO_DATA_ROOT") = fso.BuildPath(root, "DUCAI_STUDIO_DATA")
environment("DUCAI_PADDLE_PYTHON") = fso.BuildPath(root, "DUCAI_STUDIO_DATA\paddle_runtime\.venv\Scripts\python.exe")
environment("DUCAI_APP_DATA_ROOT") = fso.BuildPath(root, "RTX50_TEST_STATE")
shell.CurrentDirectory = root
WScript.Quit shell.Run(Chr(34) & executable & Chr(34), 1, True)
