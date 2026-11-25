@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if not errorlevel 1 goto use_python
echo Khong tim thay Python 3.10+ tren may. Chua cai dat bat cu phan mem nao.
goto done
:use_py
py -3 app.py
goto done
:use_python
python app.py
:done
echo.
echo Nhan phim bat ky de dong cua so nay.
pause >nul
