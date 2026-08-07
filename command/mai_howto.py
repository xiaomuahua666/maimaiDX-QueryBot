from nonebot import on_regex

how_to_use = on_regex(r'.*怎么用.*')


@how_to_use.handle()
async def _():
    await how_to_use.finish(
        '机器人帮助请前往\nhttps://wiki.awmc.team/guide/bot/intro',
    )
