# cython-lsp

一个零第三方依赖（只用 Python 标准库）的 Cython 语言服务器，供 Helix 使用。

安装位置：

```
~/.local/share/cython-lsp/
├── bin/cython-lsp      # Helix 调用的启动器
├── cython_lsp.py       # 服务端实现
├── queries/cython/     # 高亮查询源（复制到 ~/.config/helix/runtime/queries/cython/）
└── README.md
```

Helix 配置位于 `~/.config/helix/languages.toml`：

```toml
[language-server.cython-lsp]
command = "/home/connor/.local/share/cython-lsp/bin/cython-lsp"
timeout = 20

[[language]]
name = "cython"
scope = "source.cython"
file-types = ["pyx", "pxd", "pxi"]
comment-token = "#"
grammar = "python"
language-servers = ["cython-lsp"]
```

高亮使用 Helix 内置的 Python tree-sitter 语法（`grammar = "python"`），
并通过 `~/.config/helix/runtime/queries/cython/*.scm` 继承 Python 查询、
把 `cdef`、`cpdef`、`cimport` 等 Cython 关键字以及 `char`、`double`、
`size_t` 等 C 类型高亮为对应类别。

## 功能

- 语法高亮：Helix tree-sitter（`.pyx` / `.pxd` / `.pxi`）。
- 自动补全：Cython/Python 关键字、Python 内置名、`import` / `cimport`
  引入的名字、当前文件中的类/函数/方法/变量；在 `cimport ` 后补全常用
  Cython/C 模块。`from libc.string cimport ` 这类位置会从 Cython 自带的
  `.pxd` 中解析 `memset`、`memcpy`、`malloc` 等导出名和签名。
- 本地模块：`cimport mymod` / `from mymod cimport ...` 会优先在当前文件
  目录和工作区根目录查找 `mymod.pxd`，补全其中的函数、类型和类；支持
  `mymod.` 成员补全。
- Python 存根：`import mod` / `from mod import ...` 会查找项目内或已安装
  模块的 `.pyi`（没有时回退 `.py`）。hover 和补全会显示存根里的注释和
  docstring，跳转定义可以直接跳到 `.pyi` 文件。
- 错误提示：调用真正的 Cython 编译器 `cython -3` 检查当前缓冲区。
  `.pxd` 会通过一个临时 `cimport` 包装模块检查，`.pxi` 通过 `include`
  包装检查，避免直接编译声明文件产生误报。
- 额外提供 hover、跳转到定义、文档符号（`space` + `s` 等）。

模块解析顺序：当前缓冲区所在目录、工作区根目录、当前工作目录，
最后才是 Cython 自带的 `Cython/Includes`。

## 依赖

- Python 3.9+
- Cython（诊断功能需要）：

```sh
python3 -m pip install --user Cython
```

如果 Cython 不在默认 Python 环境里，可以设置：

```sh
export CYTHON_LSP_CYTHON="/path/to/python -m cython"
# 或
export CYTHON_LSP_PYTHON="/path/to/python"
```

其他环境变量：

- `CYTHON_LSP_LOG`：日志文件路径，设为空字符串可禁用日志；
  默认 `~/.cache/cython-lsp/server.log`。

## 自测

```sh
~/.local/share/cython-lsp/bin/cython-lsp --version
~/.local/share/cython-lsp/bin/cython-lsp --check path/to/file.pyx
```

在 Helix 中打开一个 `.pyx` 文件后，可以用 `:lsp-restart` 重启语言服务器，
或在终端运行 `helix --health cython` 检查服务器与高亮查询状态。
用 `:tree-sitter-highlight-name` 可查看光标处的高亮分组。

修改 `languages.toml` 后需要重启 Helix。
