"""Windows folder selection through IFileOpenDialog; no extra runtime packages."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import uuid


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]


def guid(value):
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def method(pointer, index, *arguments):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(table[index])


def check(result):
    if result < 0:
        raise OSError(f"Windows 폴더 선택 오류: 0x{result & 0xffffffff:08X}")


def choose_folder(title, initial=""):
    if os.name != "nt":
        raise OSError("폴더 선택은 Windows에서 사용할 수 있습니다. 경로를 직접 입력해 주세요.")
    ole = ctypes.OleDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    shell = ctypes.WinDLL("shell32")
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = ctypes.c_long
    dialog, selected, start, display = (ctypes.c_void_p() for _ in range(4))
    check(ole.CoInitializeEx(None, 2))  # STA; dialogs must run on an initialized thread.
    try:
        clsid = guid("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")
        iid = guid("d57c7288-d4ad-4768-be02-9d969532d960")
        shell_iid = guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog)))
        options = wintypes.DWORD()
        check(method(dialog, 10, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(options)))
        # FOS_PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR | DONTADDTORECENT
        check(method(dialog, 9, wintypes.DWORD)(dialog, options.value | 0x20 | 0x40 | 0x800 | 0x8 | 0x2000000))
        check(method(dialog, 17, wintypes.LPCWSTR)(dialog, title))
        check(method(dialog, 18, wintypes.LPCWSTR)(dialog, "폴더 선택"))
        path = Path(initial.strip().strip('"')) if isinstance(initial, str) and initial.strip() else None
        if path is not None and path.is_absolute() and path.is_dir():
            result = shell.SHCreateItemFromParsingName(str(path), None, ctypes.byref(shell_iid), ctypes.byref(start))
            if result >= 0:
                # Reopen the path shown beside the clicked button.
                check(method(dialog, 12, ctypes.c_void_p)(dialog, start))
        # A browser can belong to another process. Use an independent dialog
        # instead of making that process's window a modal owner.
        result = method(dialog, 3, wintypes.HWND)(dialog, None)
        if result & 0xffffffff == 0x800704c7:  # User cancelled; leave the form untouched.
            return None
        check(result)
        check(method(dialog, 20, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(selected)))
        check(method(selected, 5, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
            selected, 0x80058000, ctypes.byref(display)))  # SIGDN_FILESYSPATH
        return ctypes.wstring_at(display.value)
    finally:
        if display:
            ole.CoTaskMemFree(display)
        for pointer in (selected, start, dialog):
            if pointer:
                method(pointer, 2)(pointer)
        ole.CoUninitialize()
