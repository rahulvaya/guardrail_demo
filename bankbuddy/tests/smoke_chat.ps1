$ErrorActionPreference = "Stop"
$sess = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$login = Invoke-WebRequest -Uri http://localhost:8001/auth/local-dev/exchange -Method POST -ContentType 'application/json' -Body '{"username":"alice"}' -WebSession $sess -UseBasicParsing
Write-Host "login:" $login.StatusCode
$chat = Invoke-WebRequest -Uri http://localhost:8001/chat -Method POST -ContentType 'application/json' -Body '{"session_id":"s1","message":"What is my checking account balance?"}' -WebSession $sess -UseBasicParsing -TimeoutSec 120
Write-Host "chat:" $chat.StatusCode
Write-Host $chat.Content
