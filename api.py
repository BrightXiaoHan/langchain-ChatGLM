import argparse
import json
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

import fastapi
import nltk
import pydantic
import uvicorn
from fastapi import Body, FastAPI, File, Form, Query, UploadFile, WebSocket
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from typing_extensions import Annotated

from chains.local_doc_qa import LocalDocQA, load_file
from configs.model_config import (API_UPLOAD_ROOT_PATH, EMBEDDING_DEVICE,
                                  EMBEDDING_MODEL, LLM_MODEL)

nltk.data.path = [os.path.join(os.path.dirname(__file__), "nltk_data")] + nltk.data.path

# return top-k text chunk from vector store
VECTOR_SEARCH_TOP_K = 6

# LLM input history length
LLM_HISTORY_LEN = 5


class BaseResponse(BaseModel):
    code: int = pydantic.Field(200, description="HTTP status code")
    msg: str = pydantic.Field("success", description="HTTP status message")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
            }
        }


class ListDocsResponse(BaseResponse):
    data: List[str] = pydantic.Field(..., description="List of document names")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
                "data": ["doc1.docx", "doc2.pdf", "doc3.txt"],
            }
        }


class UploadFilesResponse(BaseModel):
    segments: Dict[str, List[str]] = pydantic.Field(
        ..., description="Segment results of uploaded files"
    )

    class Config:
        schema_extra = {
            "example": {
                "segments": {
                    "doc1.docx": [
                        "第一段",
                        "第二段",
                        "第三段",
                    ],
                    "doc2.pdf": [
                        "第一段",
                        "第二段",
                        "第三段",
                    ],
                }
            }
        }


class SearchResponse(BaseResponse):
    class SearchResult(BaseModel):
        docname: str = pydantic.Field(..., description="Document name or id")
        content: str = pydantic.Field(..., description="Result content")
        matched_terms: List[str] = pydantic.Field(..., description="Matched terms")
        bm25_score: float = pydantic.Field(..., description="BM25 score")

    data: List[SearchResult] = pydantic.Field(..., description="Search results")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
                "data": [
                    {
                        "docname": "广州市单位从业的特定人员参加工伤保险办事指引.docx",
                        "content": '八)  社会<b class="match term0">保险</b>经办机构业务部门...通过自查方式发现错  误审核<b class="match term1">工伤保险</b>待遇和<b class="match term1">工伤保险</b>基金支出的，  应当重新审核， 少发<b class="match term1">工伤保险</b>待遇或者<b class="match term1">工伤保险</b>基金支出的，  给予补发；  多  发<b class="match term1">工伤保险</b>待遇或者<b class="match term1">工伤保险</b>基金支出的，  应当追回',
                        "matched_terms": ["工伤", "保险", "工伤保险"],
                    },
                    {
                        "docname": "广州市单位从业的特定人员参加工伤保险办事指引.docx",
                        "content": '办理<b class="match term1">工伤保险</b>参保手续后未按规定 及时缴纳<b class="match term1">工伤保险</b>费的期间，  <b class="match term1">工伤保险</b>关系暂不生效，   自实 际缴纳<b class="match term1">工伤保险</b>费的次日起生效',
                        "matched_terms": ["工伤", "保险", "工伤保险"],
                    },
                ],
            }
        }


class ChatMessage(BaseModel):
    class SourceDocument(BaseModel):
        content: str = pydantic.Field(..., description="Document content")
        raw_content: str = pydantic.Field(..., description="Document raw content")
        score: float = pydantic.Field(..., description="Score")
        filename: str = pydantic.Field(..., description="Document name")
        category: str = pydantic.Field(..., description="Document category")

    question: str = pydantic.Field(..., description="Question text")
    source_documents: List[SourceDocument] = pydantic.Field(
        ..., description="List of source documents and their scores"
    )

    class Config:
        schema_extra = {
            "example": {
                "question": "工伤保险如何办理？",
                "source_documents": [
                    {
                        "content": "(一)  从业单位  (组织)  按“自愿参保”原则，  为未建 立劳动关系的特定从业人员单项参加工伤保险 、缴纳工伤保 险费。",
                        "raw_content": "不包含标签和前后文的原始匹配到的文本",
                        "score": 0.9999999403953552,
                        "filename": "doc1.docx",
                        "category": "Title",
                    },
                ],
            }
        }


class SummaryRequest(BaseModel):
    history: List[List[str]] = pydantic.Field(..., description="History text")

    class Config:
        schema_extra = {
            "example": [
                [
                    "工伤保险是什么？",
                    "工伤保险是指用人单位按照国家规定，为本单位的职工和用人单位的其他人员，缴纳工伤保险费，由保险机构按照国家规定的标准，给予工伤保险待遇的社会保险制度。",
                ]
            ],
        }

    @pydantic.validator("history")
    def check_history_length(cls, v):
        for item in v:
            if len(item) != 2:
                raise fastapi.HTTPException(
                    status_code=400,
                    detail="History text should be a list of (question, answer) pairs",
                )
        return v


class SummaryResponse(BaseResponse):
    class SummaryResult(BaseModel):
        summary: str = pydantic.Field(..., description="Summary content")

    data: SummaryResult = pydantic.Field(..., description="Summary results")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
                "data": {
                    "summary": "办理工伤保险参保手续后未按规定 及时缴纳工伤保险费的期间，  工伤保险关系暂不生效，   自实 际缴纳工伤保险费的次日起生效",
                },
            }
        }


def get_folder_path(local_doc_id: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id)


def get_vs_path(local_doc_id: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id, "vector_store")


def get_ti_path(local_doc_id: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id, "text_index")


def get_file_path(local_doc_id: str, doc_name: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id, doc_name)


async def upload_file(
    files: Annotated[
        List[UploadFile], File(description="Multiple files as UploadFile")
    ],
    knowledge_base_id: str = Form(
        ..., description="Knowledge Base Name", example="kb1"
    ),
):
    segments = {}
    saved_path = get_folder_path(knowledge_base_id)
    if not os.path.exists(saved_path):
        os.makedirs(saved_path)
    for file in files:
        file_path = os.path.join(saved_path, file.filename)
        with open(file_path, "wb") as f:
            f.write(file.file.read())
        segs = load_file(file_path)
        segments[file.filename] = [seg.page_content for seg in segs]

    local_doc_qa.init_knowledge_vector_store(saved_path, get_vs_path(knowledge_base_id))
    local_doc_qa.init_text_indexing(saved_path, get_ti_path(knowledge_base_id))
    return UploadFilesResponse(
        segments=segments,
    )


async def list_docs(
    knowledge_base_id: Optional[str] = Query(
        description="Knowledge Base Name", example="kb1"
    )
):
    if knowledge_base_id:
        local_doc_folder = get_folder_path(knowledge_base_id)
        if not os.path.exists(local_doc_folder):
            return {"code": 1, "msg": f"Knowledge base {knowledge_base_id} not found"}
        all_doc_names = [
            doc
            for doc in os.listdir(local_doc_folder)
            if os.path.isfile(os.path.join(local_doc_folder, doc))
        ]
        return ListDocsResponse(data=all_doc_names)
    else:
        if not os.path.exists(API_UPLOAD_ROOT_PATH):
            all_doc_ids = []
        else:
            all_doc_ids = [
                folder
                for folder in os.listdir(API_UPLOAD_ROOT_PATH)
                if os.path.isdir(os.path.join(API_UPLOAD_ROOT_PATH, folder))
            ]

        return ListDocsResponse(data=all_doc_ids)


async def delete_docs(
    knowledge_base_id: str = Form(
        ..., description="Knowledge Base Name", example="kb1"
    ),
    doc_name: Optional[str] = Form(
        None, description="doc name", example="doc_name_1.pdf"
    ),
):
    if not os.path.exists(os.path.join(API_UPLOAD_ROOT_PATH, knowledge_base_id)):
        return {"code": 1, "msg": f"Knowledge base {knowledge_base_id} not found"}
    if doc_name:
        doc_path = get_file_path(knowledge_base_id, doc_name)
        if os.path.exists(doc_path):
            os.remove(doc_path)
        else:
            return {"code": 1, "msg": f"document {doc_name} not found"}

        remain_docs = await list_docs(knowledge_base_id)
        if remain_docs["code"] != 0 or len(remain_docs["data"]) == 0:
            shutil.rmtree(get_folder_path(knowledge_base_id), ignore_errors=True)
        else:
            local_doc_qa.init_knowledge_vector_store(
                get_folder_path(knowledge_base_id), get_vs_path(knowledge_base_id)
            )
            local_doc_qa.init_text_indexing(
                get_folder_path(knowledge_base_id), get_ti_path(knowledge_base_id)
            )
    else:
        shutil.rmtree(get_folder_path(knowledge_base_id))
    return BaseResponse()


PROMPT_TEMPLATE = """对话记录：
{context} 

请将上述对话记录的关键信息提取出来，要进行总结不要照抄对话，不要漏掉关键信息，不要编造额外信息："""


async def summary(body: SummaryRequest = Body(..., embed=False)):
    history = body.history
    context = "\n".join([f"话务员：{q}\n市民：{a}" for q, a in history])
    prompt = PROMPT_TEMPLATE.format(context=context)
    for summary, _ in local_doc_qa.llm._call(prompt=prompt, streaming=True):
        pass
    return SummaryResponse(data={"summary": summary})


async def chat(
    knowledge_base_id: str = Body(
        ..., description="Knowledge Base Name", example="kb1"
    ),
    question: str = Body(..., description="Question", example="工伤保险是什么？"),
):
    vs_path = os.path.join(API_UPLOAD_ROOT_PATH, knowledge_base_id, "vector_store")
    if not os.path.exists(vs_path):
        raise fastapi.HTTPException(
            status_code=404, detail=f"Knowledge base {knowledge_base_id} not found"
        )

    source_documents = []
    ti_path = get_ti_path(knowledge_base_id)
    results = local_doc_qa.search(
        query=question, ti_path=ti_path, top_k=VECTOR_SEARCH_TOP_K * 2
    )

    for item in results:
        source_documents.append(
            {
                "content": item["content"],
                "raw_content": item["raw_content"],
                "score": item["score"],
                "filename": item["docname"],
                "category": "Title",
            },
        )

    # 如果问题过短，不再进行dense search
    if len(question) >= 5:
        dense_source_documents = []
        dense_results = local_doc_qa.dense_search(query=question, vs_path=vs_path)
        for item in dense_results:
            if item.metadata["raw_content"] in [
                doc["raw_content"] for doc in source_documents
            ]:
                index = [doc["raw_content"] for doc in source_documents].index(
                    item.metadata["raw_content"]
                )
                source_documents[index]["score"] = 0.5 * (
                    item.metadata["score"] + source_documents[index]["score"]
                )
            else:
                dense_source_documents.append(
                    {
                        "content": item.page_content,
                        "score": item.metadata["score"],
                        "filename": item.metadata["filename"],
                        "category": item.metadata["category"],
                        "raw_content": item.metadata["raw_content"],
                    },
                )

        source_documents += dense_source_documents
    source_documents = sorted(source_documents, key=lambda x: x["score"], reverse=True)
    source_documents = list(filter(lambda x: x["score"] > 50, source_documents))
    source_documents = source_documents[:VECTOR_SEARCH_TOP_K]

    return ChatMessage(
        question=question,
        source_documents=source_documents,
    )


async def stream_chat(websocket: WebSocket, knowledge_base_id: str):
    await websocket.accept()
    vs_path = os.path.join(API_UPLOAD_ROOT_PATH, knowledge_base_id, "vector_store")

    if not os.path.exists(vs_path):
        await websocket.send_json(
            {"error": f"Knowledge base {knowledge_base_id} not found"}
        )
        await websocket.close()
        return

    history = []
    turn = 1
    while True:
        question = await websocket.receive_text()
        await websocket.send_json({"question": question, "turn": turn, "flag": "start"})

        last_print_len = 0
        for resp, history in local_doc_qa.get_knowledge_based_answer(
            query=question, vs_path=vs_path, chat_history=history, streaming=True
        ):
            await websocket.send_text(resp["result"][last_print_len:])
            last_print_len = len(resp["result"])

        source_documents = []
        for doc in resp["source_documents"]:
            source_documents.append(
                {
                    "content": doc.page_content,
                    "score": float(doc.metadata["score"]),
                    "filename": doc.metadata["filename"],
                    "category": doc.metadata["category"],
                }
            )

        await websocket.send_text(
            json.dumps(
                {
                    "question": question,
                    "turn": turn,
                    "flag": "end",
                    "sources_documents": source_documents,
                },
                ensure_ascii=False,
            )
        )
        turn += 1


async def search(
    knowledge_base_id: str = Body(
        ..., example="kb1", description="Knowledge Base Name"
    ),
    query: str = Body(..., example="工伤保险是什么？", description="Search query"),
    top_k: int = Body(5, example=5, description="Number of results to return"),
):
    ti_path = get_ti_path(knowledge_base_id)
    if not os.path.exists(ti_path):
        raise fastapi.HTTPException(
            status_code=404, detail=f"Knowledge base {knowledge_base_id} not found"
        )

    results = local_doc_qa.search(query=query, ti_path=ti_path, top_k=top_k)
    return SearchResponse(data=results)


def gen_docs():
    global app
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as f:
        json.dump(
            get_openapi(
                title=app.title,
                version=app.version,
                openapi_version=app.openapi_version,
                description=app.description,
                routes=app.routes,
            ),
            f,
            ensure_ascii=False,
        )
        f.flush()
        # test whether widdershins is available
        try:
            subprocess.run(
                [
                    "widdershins",
                    f.name,
                    "-o",
                    os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "docs",
                        "API.md",
                    ),
                ],
                check=True,
            )
        except Exception:
            raise RuntimeError(
                "Failed to generate docs. Please install widdershins first."
            )


def main():
    global app
    global local_doc_qa
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--gen-docs", action="store_true")
    args = parser.parse_args()

    app = FastAPI()
    app.websocket("/chat-docs/stream-chat/{knowledge_base_id}")(stream_chat)
    app.post("/chat-docs/chat", response_model=ChatMessage)(chat)
    app.post("/chat-docs/upload", response_model=BaseResponse)(upload_file)
    app.post("/chat-docs/search", response_model=SearchResponse)(search)
    app.post("/chat-docs/summary", response_model=SummaryResponse)(summary)
    app.get("/chat-docs/list", response_model=ListDocsResponse)(list_docs)
    app.delete("/chat-docs/delete", response_model=BaseResponse)(delete_docs)

    if args.gen_docs:
        gen_docs()
        return

    local_doc_qa = LocalDocQA()
    local_doc_qa.init_cfg(
        llm_model=LLM_MODEL,
        embedding_model=EMBEDDING_MODEL,
        embedding_device=EMBEDDING_DEVICE,
        llm_history_len=LLM_HISTORY_LEN,
        top_k=VECTOR_SEARCH_TOP_K,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
