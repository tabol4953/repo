### 使用流程介绍

**注意:** 以下说明中包含 {*} 的内容均代表变量

1. repo 引导命令下载
2. repo init 初始化
3. repo sync 仓库同步
4. repo + gitee 本地开发流程

### 前提条件

1. 注册码云gitee帐号。

2. 注册码云SSH公钥，请参考[码云帮助中心](https://gitee.com/help/articles/4191)。

3. 安装[git客户端](https://git-scm.com/book/zh/v2/%E8%B5%B7%E6%AD%A5-%E5%AE%89%E8%A3%85-Git)和[git-lfs](https://gitee.com/vcs-all-in-one/git-lfs?_from=gitee_search#downloading)并配置用户信息。

   ```shell
   git config --global user.name "yourname"
   git config --global user.email "your-email-address"
   git config --global credential.helper store
   ```

### 1. Repo 引导命令安装

```shell
# python3 版本向下兼容，注意这里应该下载是 repo-py3，而不是 repo
# PS: 这里下载的 repo 只是一个引导脚本，需要后续 repo init 后才有完整功能. ps 如果环境中有repo命令，可跳过
curl https://github.com/tabol4953/repo/raw/master/repo-py3 > ~/bin/repo
# 赋予脚本可执行权限
chmod a+x ~/bin/repo
# 编辑环境变量
vim ~/.bashrc               
export PATH=~/bin:$PATH     # 在环境变量的最后添加一行repo路径信息
source ~/.bashrc            # 应用环境变量

# 安装 requests 依赖，如果跳过这一步，后续执行命令时会自动提示安装
pip3 install -i https://repo.huaweicloud.com/repository/pypi/simple requests
```

### 2. Repo 初始化

```shell
mkdir your_project && cd your_project
repo init -u https://github.com/openharmony/manifest -b {branch} -m {manifest_xml} --no-repo-verify
```

### 3. Repo 仓库同步
```shell
repo  sync -c
repo forall -c 'git lfs pull'
```

### 4. Repo + Gitee 本地开发流程
```shell
repo start {branch} --all # 切换开发分支，当对部分仓库进行指定时，会触发仓库的预先fork
repo forall -c git add ./ git add  # 批量加入暂存区或者单独加入
repo forall -c git commit -m --signoff {msg} / git commit  # 批量进行提交或者单独提交
repo config --global repo.token {TOKEN} # 进行 gitee access_token 配置, access_token 获取连接 https://gitee.com/profile/personal_access_tokens
repo config repo.pullrequest {True/False} # 对是否触发PR进行配置
repo push --br={BRANCH} --d={DEST_BRANCH} --title {title} # 进行推送并生成PR和审查，执行后会展示出可进行推送的项目，去掉注释的分支会进行后续推送
repo gitee-pr --br={BRANCH} # 获取项目推送后的指定分支的PR列表
```
