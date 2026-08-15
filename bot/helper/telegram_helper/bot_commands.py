from ...core.config_manager import Config

i = Config.CMD_SUFFIX

class BotCommands:
    StartCommand = f"start{i}"
    LeechCommand = [f"leech{i}", f"l{i}"]
    QbLeechCommand = [f"qbleech{i}", f"ql{i}"]
    YtdlLeechCommand = [f"ytdlleech{i}", f"yl{i}"]
    CancelTaskCommand = [f"cancel{i}", f"c{i}"]
    CancelAllCommand = f"cancelall{i}"
    ForceStartCommand = [f"forcestart{i}", f"fs{i}"]
    SearchCommand = f"search{i}"
    StatusCommand = f"status{i}"
    UsersCommand = f"users{i}"
    AuthorizeCommand = f"auth{i}"
    UnAuthorizeCommand = f"unauth{i}"
    AddSudoCommand = f"addsudo{i}"
    RmSudoCommand = f"rmsudo{i}"
    PingCommand = f"ping{i}"
    RestartCommand = f"restart{i}"
    StatsCommand = f"stats{i}"
    HelpCommand = f"help{i}"
    LogCommand = f"log{i}"
    ShellCommand = f"shell{i}"
    AExecCommand = f"aexec{i}"
    ExecCommand = f"exec{i}"
    ClearLocalsCommand = f"clearlocals{i}"
    BotSetCommand = [f"bsetting{i}", f"bs{i}"]
    UserSetCommand = [f"usetting{i}", f"us{i}"]
    SelectCommand = f"sel{i}"
    RssCommand = f"rss{i}"
    BypassCommand = [f"bypass{i}", f"bp{i}"]
    LoginCommand = f"login{i}"
    LogoutCommand = f"logout{i}"
