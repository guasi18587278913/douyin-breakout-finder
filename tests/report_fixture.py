"""Synthetic selection-report responses for protocol and browser checks."""

def selection_answer(packet):
    chosen = packet.get('featured_candidates', [])[:2]
    lessons = []
    for vid in chosen:
        evidence = packet['evidence'][vid]
        entry = next((e for e in evidence if e['kind'] == 'transcript'), evidence[0])
        lessons.append({'title': '把一个具体任务讲清楚',
                        'cases': [{'video_id': vid,
                                   'paragraphs': ['这条合成案例围绕重复录入这个具体问题，帮助用户理解任务。',
                                                  '值得参考的是把输入、操作与检查连在一起，让读者知道方法用在哪里。'],
                                   'evidence_ids': [entry['id']]}],
                        'applications': [{'audience': '办公教程', 'idea': '同一份资料，怎样减少反复录入？'}],
                        'takeaway': '围绕一个真实任务解释方法，再让读者判断是否适合自己的工作。'})
    return {'format': 'selection-report-v1', 'topic_label': packet.get('topic', '合成主题'),
            'title': '自动生成的离线合成报告', 'intro': ['这些合成案例用于检查报告生成流程。'],
            'lessons': lessons, 'closing': {'questions': ['用户眼前具体要完成什么任务？'],
                                         'paragraphs': ['把例子换成自己的实际任务，核对结果再决定是否继续使用。']},
            'limitations': ['全部为合成测试数据，不是真实调研。']}
