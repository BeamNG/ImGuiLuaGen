# ImGui LuaJIT FFI wrapper Generator


This is a python3 script that generates LuaJIT FFI bindings and lua wrappers by parsing the imgui header file via clang.


Used in BeamNG to generate the ImGui wrapper

1) install all the dependencies:

   * on Ubuntu 20.04:
       ```bash
       apt install python3-pip clang libclang-dev libclang-6.0-dev
       pip3 install clang
       ```
   * on Windows 10:
       ensure python3 and pip are installed and usable from command line
       ```bash
       pip3 install clang
       ```
       install LLVM to the default install path, using a prebuilt *win64.exe installer from: https://github.com/llvm/llvm-project/releases

2) run it like this:

   ```bash
   python gen.py imgui/imgui.h
   ```