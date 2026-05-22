$ErrorActionPreference = 'Stop'

ssh.exe -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R 18080:127.0.0.1:8080 aulamovil@192.168.0.169
