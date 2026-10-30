# Seguridad

## Qué hace y qué no hace iRacing Optimizer

| | |
|---|---|
| **Red** | No se conecta a internet. No envía datos ni telemetría a ningún sitio. |
| **Permisos** | No necesita ni pide permisos de administrador. |
| **Lee** | Los `.ini` de `Documentos\iRacing`, la telemetría en vivo de iRacing (memoria compartida, solo lectura) y datos del sistema (CPU, RAM, GPU vía driver NVIDIA, procesos en ejecución). |
| **Escribe** | Solo en `Documentos\iRacing` (los `.ini` de iRacing que están en una lista cerrada, y sus copias de seguridad en `iroptimizer_backups`) y en `Documentos\iRacingOptimizer` (sesiones, informes y perfiles). |
| **Ejecuta** | Solo `powercfg.exe` y `nvidia-smi.exe`, siempre desde su ruta del sistema (nunca desde la carpeta actual). |

## Protecciones

- **Antes de modificar cualquier `.ini` se hace una copia de seguridad.** La escritura es atómica: si algo falla a mitad, el archivo original queda intacto.
- **Solo se modifican claves que ya existen** en tus archivos. No se crean claves ni secciones nuevas.
- **Lista blanca de archivos**: solo `rendererDX11*.ini`, `app.ini` y `core.ini` de la carpeta de iRacing. Un perfil o un JSON no puede apuntar a otro archivo ni salir de la carpeta.
- **Valores validados**: lo que se escribe en un `.ini` solo puede contener letras, números, espacio, `.`, `_`, `+` y `-`, con un máximo de 64 caracteres. Así un perfil compartido no puede inyectar líneas, secciones ni comentarios.
- **Perfiles importados**: se limpian al importarlos y se descarta lo que no parezca un ajuste válido. La app te avisa de cuántos valores ha descartado.
- **Informes HTML**: todo el texto que procede de sesiones (nombres de circuito, coches, procesos…) se escapa, así que una sesión manipulada no puede inyectar código en el informe.
- **Restaurar copias**: solo restaura `.ini` conocidos y no admite rutas.

## Privacidad al compartir sesiones

Los archivos `session_*.json` incluyen tu hardware, tu configuración de iRacing y los nombres de los procesos que más CPU consumieron mientras conducías. La ruta de tu carpeta se guarda como `%USERPROFILE%\…`, sin tu nombre de usuario de Windows. Revisa el archivo antes de publicarlo si te preocupa la lista de procesos.

## Verificar la descarga

Cada release incluye `SHA256SUMS.txt`. En PowerShell:

```powershell
Get-FileHash .\iRacingOptimizer.exe -Algorithm SHA256
```

El valor tiene que coincidir con el de `SHA256SUMS.txt`. El ejecutable no está firmado digitalmente, así que Windows SmartScreen puede mostrar un aviso. El `.exe` se compila en GitHub Actions a partir del código de este repositorio, con dependencias de versión fija y acciones fijadas por commit. En cada compilación se pasan los tests, el análisis estático (bandit) y la auditoría de dependencias (pip-audit).

## Avisar de un problema

Si encuentras un fallo de seguridad, abre un *issue* sin detalles explotables y pide un canal privado, o usa *Security → Report a vulnerability* en GitHub.
