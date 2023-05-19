import os

import fastapi
import pandas
import pycorrector
import pydantic
from jieba.analyse import ChineseAnalyzer
from whoosh.fields import ID, TEXT, Schema
from whoosh.index import create_in, open_dir
from whoosh.qparser import OrGroup, QueryParser


def get_hardcoding_scripts_path():
    return os.path.join(
        os.path.dirname(__file__), "hardcoding_scripts", "hardcoding_scripts.xlsx"
    )


def get_hardcoding_scripts_index_path():
    return os.path.join(os.path.dirname(__file__), "hardcoding_scripts", "index")


def init_text_indexing():
    schema = Schema(
        path=TEXT(stored=True),
        uid=ID(stored=True),
        content=TEXT(stored=True, analyzer=ChineseAnalyzer()),
    )
    if not os.path.exists(get_hardcoding_scripts_index_path()):
        os.mkdir(get_hardcoding_scripts_index_path())
    ix = create_in(get_hardcoding_scripts_index_path(), schema)
    writer = ix.writer()
    # load all sheets in excel
    filepath = get_hardcoding_scripts_path()
    sheets = pandas.read_excel(filepath, sheet_name=None, skiprows=[0])
    for sheet_name, sheet in sheets.items():
        sheet.fillna("", inplace=True)
        if "编号" not in sheet.columns or "话术" not in sheet.columns:
            continue
        for index, content in zip(sheet["编号"], sheet["话术"]):
            try:
                index = str(int(index))
            except ValueError:
                continue
            content = str(content).strip()
            if not content:
                continue
            writer.add_document(
                path=sheet_name,
                uid=index,
                content=content,
            )
    writer.commit()
    

class HardcodingResponse(pydantic.BaseModel):
    uid: str = pydantic.Field(..., description="编号")
    content: str = pydantic.Field(..., description="话术")

    class Config:
        schema_extra = {
            "example": {
                "uid": "100",
                "content": "您好，我是小智，很高兴为您服务，您可以问我关于产品的任何问题哦~",
            }
        }


async def match_hardcoding_scripts(
    query: str = fastapi.Body(..., max_length=100, description="用户输入的文本", embed=False)
):
    query, _ = pycorrector.correct(query)
    ix = open_dir(get_hardcoding_scripts_index_path())
    hits = []
    with ix.searcher() as searcher:
        query = QueryParser("content", ix.schema, group=OrGroup).parse(query)
        query_len = len(query) if isinstance(query, list) else 1
        results = searcher.search(query, limit=5, terms=True)
        for hit in results:
            matched_terms = [item[1].decode() for item in hit.matched_terms()]
            hits.append(
                {
                    "path": hit["path"],
                    "uid": hit["uid"],
                    "content": hit["content"],
                    "matched_terms": matched_terms,
                    "bm25_score": hit.score,
                    "score": (len(matched_terms) / query_len) * 100,
                }
            )

    hits = list(filter(lambda x: x["score"] > 80, hits))
    hits = sorted(hits, key=lambda x: x["score"], reverse=True)
    return HardcodingResponse(
        uid=hits[0]["uid"] if hits else "",
        content=hits[0]["content"] if hits else "",
    )
