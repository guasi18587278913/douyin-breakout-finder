"""Lossless handling of JSON formatting mistakes; never invents missing content."""
import json
import re


class ModelJSONError(ValueError):
    def __init__(self, kind, position=None, line=None, column=None):
        super().__init__(kind)
        self.detail={'kind':kind,'position':position,'line':line,'column':column}


def without_trailing_commas(text):
    output=[];quoted=False;escaped=False;changed=False
    for index,char in enumerate(text):
        if quoted:
            output.append(char)
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        else:
            if char=='"':quoted=True
            if char==',':
                look=index+1
                while look<len(text) and text[look].isspace():look+=1
                back=index-1
                while back>=0 and text[back].isspace():back-=1
                prior=text[back] if back>=0 else ''
                if look<len(text) and text[look] in ']}' and prior and prior not in '[{,:':
                    changed=True;continue
            output.append(char)
    return ''.join(output),changed


def decode_object(content):
    if not isinstance(content,str):raise ModelJSONError('non_text_content')
    text=content.strip();changes=[]
    if text.startswith('\ufeff'):text=text.lstrip('\ufeff').strip();changes.append('bom')
    fenced=re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```',text,re.S|re.I)
    if fenced:text=fenced.group(1);changes.append('code_fence')
    text,commas=without_trailing_commas(text)
    if commas:changes.append('trailing_commas')
    def pairs(values):
        out={}
        for key,value in values:
            if key in out:raise ModelJSONError('duplicate_key')
            out[key]=value
        return out
    def constant(value):raise ModelJSONError('non_finite_number')
    try:
        # Literal newlines/tabs in quoted paragraphs are preserved exactly.
        answer=json.loads(text,strict=False,object_pairs_hook=pairs,parse_constant=constant)
    except json.JSONDecodeError as error:
        raise ModelJSONError('invalid_json',error.pos,error.lineno,error.colno) from None
    if not isinstance(answer,dict):raise ModelJSONError('non_object_root')
    def valid_controls(value):
        if isinstance(value,str):return all(ord(c)>=32 or c in '\t\r\n' for c in value)
        if isinstance(value,list):return all(valid_controls(v) for v in value)
        if isinstance(value,dict):return all(valid_controls(k) and valid_controls(v) for k,v in value.items())
        return True
    if not valid_controls(answer):raise ModelJSONError('unsupported_control_character')
    return answer,changes
