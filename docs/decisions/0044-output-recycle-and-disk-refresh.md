# ADR-0044：成品回收与磁盘状态刷新

日期：2026-09-27。状态：采用。

- 已有正式成品和旧历史成品可单独移入系统回收站；不回收原视频、字幕、音轨、试片、目录、未登记文件或项目外路径。不自动清理旧成品。
- Artifact 登记及发布草稿引用保留。状态查询对合成产物做轻量 stat，返回磁盘可用性和文件修订标记；缺失文件从可用成品、默认预览和覆盖提示中排除，折叠列出缺失记录。原位还原后刷新即可重新使用，无须重建索引或合成。
- 同组合的新成品生成后，不再因保留的旧历史文件缺失而永远过期；缓存完整性只校验同组合最近登记版本，最新版本缺失仍视为不完整。不修改其他制作步骤的状态或指纹。
- 回收前明确确认一个文件的名称和版本。后端重新验证产物 ID、实际路径及 stat 修订，旧弹窗不能删除已被替换的新文件。合成入队锁与发布锁保护回收期间；上传暂停但后台尚未退出时仍禁止操作。先释放本页预览，再发出回收请求。
- Windows 采用 IFileOperation 的 RECYCLEONDELETE / EARLYFAILURE 标记及 PreDeleteItem 回调；回调必须抛 COM 失败 HRESULT 来拒绝永久删除，不能仅返回一个普通 Python 整数。增加 Windows 条件依赖 pywin32，不退化使用旧的静默删除入口。其他平台使用既有 send2trash；错误只报告失败，不补做 unlink。
- 发布页的文件刷新仅更新候选文件及缺失状态，不删除选中分P，不覆盖本地未保存的发布资料，也不修改已经上传或投稿的远程文件。

依据：[Windows 回收操作标记](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags)、[删除前回调的中止语义](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperationprogresssink-predeleteitem)。

测试使用隔离文件与模拟回收站/COM，不向真实回收站放入测试文件，不删除用户成品。覆盖回收、原位恢复、外部缺失、确认后替换、非法路径、回收失败、合成/上传/收尾占用，以及刷新保留未保存草稿。
