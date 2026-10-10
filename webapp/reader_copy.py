"""Reader-facing prose rules for new reports; historical request text stays immutable."""
import re
from core import ValidationIssue


def reader_instructions(system):
    replacements={
        '系统会用真实数据生成开场统计和案例播放段落，intro 不重复写这些统计。':
            '统计只在作品卡片展示；intro直接写创作者的具体困难和选题发现。',
        '顺序：基于真实数据的开场 → 本轮发现 →':
            '顺序：创作者的具体困难与选题发现 →',
        '完整结果池目标是20–30篇；精选达到2–3篇不代表数量达标，少于20篇须明确说明。':
            '数量目标只用于后台核验，正文不汇报是否达标。',
        '例：这两条讲 Skill 的视频，作者粉丝都不到 4000，播放量却都超过了 30 万。':
            '例：工具看了很多，轮到自己做课件，却不知道先试哪一个具体效果。',
        'intro 用一到两段接住程序的数据开场':
            'intro 用一到两段直接进入创作者的具体困难和选题发现',
        '开场里的统计':'作品卡片内的统计',
    }
    for old,new in replacements.items():system=system.replace(old,new)
    return system+('\n面向读者的正文不写内部流程说明：不汇报合格作品数、精选篇数、方法个数、'
        '结果池数量目标或是否达标，不写查询次数、费用预算、审核流程、目录跳转、通用依据声明。'
        '这些只保留在后台记录。intro直接进入内容分析，不再写粉丝和播放量的对比开场。'
        '具体案例需要限定依据时，可以简短说“从标题和简介看”；不能因此编造视频内容。'
        '讨论创作本身的低预算做法、选题中的数量和教程步骤是允许的。')


INTERNAL_COPY=re.compile(
    r'结果池(?:数量)?目标|(?:本轮|本次|本报告)(?:的)?结果池|篇合格作品|条合格作品|篇精选解读|预算与数据边界|'
    r'(?:^|\n)\s*[0-9一二两三四五六七八九十]+\s*个选题方法\s*[。.!！]?\s*(?:$|\n)|'
    r'本轮共找到|本次共找到|(?:本轮|本次)(?:查询|请求|模型)(?:次数|费用|预算)|'
    r'(?:本轮|本次)(?:已|共|总共|一共|累计)?(?:查询|请求|调用)了?\s*[0-9一二两三四五六七八九十百千万]+\s*次|'
    r'(?:本轮|本次)(?:数据(?:查询)?|模型(?:调用)?|API)[^。\n]{0,12}(?:预算|费用|成本|上限)|'
    r'(?:本报告|本次报告)(?:已经|已)?(?:通过|完成)(?:模型|程序|系统|自动|内容)?(?:核验|审核|验证)|'
    r'未将推测当作原视频|本报告依据|数据来源与解读依据|'
    r'(?:报告概览|精选解读|更多作品)\s*[→/｜|]\s*(?:精选解读|更多作品|数据说明)')


def validate_reader_copy(report):
    def visit(value,path):
        if isinstance(value,str) and INTERNAL_COPY.search(value):
            raise ValidationIssue(path,'删除内部数量、目标、费用、流程或通用声明；直接解释选题，不改案例与引用。')
        if isinstance(value,list):
            for index,item in enumerate(value):visit(item,path+'[%s]'%index)
        elif isinstance(value,dict):
            for key,item in value.items():
                if key not in ('video_id','evidence_ids'):visit(item,path+'.'+key)
    visit(report,'$')
