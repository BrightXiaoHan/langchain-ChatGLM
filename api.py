import json
import os
import shutil
from typing import List, Optional

import nltk
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, WebSocket
from typing_extensions import Annotated

from chains.local_doc_qa import LocalDocQA
from configs.model_config import (API_UPLOAD_ROOT_PATH, EMBEDDING_DEVICE,
                                  EMBEDDING_MODEL, LLM_MODEL)

nltk.data.path = [os.path.join(os.path.dirname(__file__), "nltk_data")] + nltk.data.path

# return top-k text chunk from vector store
VECTOR_SEARCH_TOP_K = 6

# LLM input history length
LLM_HISTORY_LEN = 3


def get_folder_path(local_doc_id: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id)


def get_vs_path(local_doc_id: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id, "vector_store")


def get_file_path(local_doc_id: str, doc_name: str):
    return os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id, doc_name)


async def upload_file(
    files: Annotated[
        List[UploadFile], File(description="Multiple files as UploadFile")
    ],
    local_doc_id: str = Form(...),
):
    saved_path = get_folder_path(local_doc_id)
    if not os.path.exists(saved_path):
        os.makedirs(saved_path)
    for file in files:
        file_path = os.path.join(saved_path, file.filename)
        with open(file_path, "wb") as f:
            f.write(file.file.read())

    local_doc_qa.init_knowledge_vector_store(saved_path, get_vs_path(local_doc_id))
    return {"code": 0, "msg": "success"}


async def list_docs(local_doc_id: Optional[str] = None):
    if local_doc_id:
        local_doc_folder = get_folder_path(local_doc_id)
        if not os.path.exists(local_doc_folder):
            return {"code": 1, "msg": f"document {local_doc_id} not found"}
        all_doc_names = [
            doc
            for doc in os.listdir(local_doc_folder)
            if os.path.isfile(os.path.join(local_doc_folder, doc))
        ]
        return {"code": 0, "msg": "success", "data": all_doc_names}
    else:
        if not os.path.exists(API_UPLOAD_ROOT_PATH):
            all_doc_ids = []
        else:
            all_doc_ids = [
                folder
                for folder in os.listdir(API_UPLOAD_ROOT_PATH)
                if os.path.isdir(os.path.join(API_UPLOAD_ROOT_PATH, folder))
            ]

        return {"code": 0, "msg": "success", "data": all_doc_ids}


async def delete_docs(local_doc_id: str, doc_name: Optional[str] = None):
    if not os.path.exists(os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id)):
        return {"code": 1, "msg": f"document {local_doc_id} not found"}
    if doc_name:
        doc_path = get_file_path(local_doc_id, doc_name)
        if os.path.exists(doc_path):
            os.remove(doc_path)
        else:
            return {"code": 1, "msg": f"document {doc_name} not found"}

        remain_docs = await list_docs(local_doc_id)
        if remain_docs["code"] != 0 or len(remain_docs["data"]) == 0:
            shutil.rmtree(get_folder_path(local_doc_id), ignore_errors=True)
        else:
            local_doc_qa.init_knowledge_vector_store(
                get_folder_path(local_doc_id), get_vs_path(local_doc_id)
            )
    else:
        shutil.rmtree(get_folder_path(local_doc_id))
    return {"code": 0, "msg": "success"}


async def stream_chat(websocket: WebSocket, local_doc_id: str):
    await websocket.accept()
    vs_path = os.path.join(API_UPLOAD_ROOT_PATH, local_doc_id, "vector_store")

    if not os.path.exists(vs_path):
        await websocket.send_json({"error": f"document {local_doc_id} not found"})
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

        source_documents = [
            f"""出处 [{inum + 1}] {os.path.split(doc.metadata['source'])[-1]}：\n\n{doc.page_content}\n\n"""
            f"""相关度：{doc.metadata['score']}\n\n"""
            for inum, doc in enumerate(resp["source_documents"])
        ]

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


def main():
    global app
    global local_doc_qa
    local_doc_qa = LocalDocQA()
    local_doc_qa.init_cfg(
        llm_model=LLM_MODEL,
        embedding_model=EMBEDDING_MODEL,
        embedding_device=EMBEDDING_DEVICE,
        llm_history_len=LLM_HISTORY_LEN,
        top_k=VECTOR_SEARCH_TOP_K,
    )
    app = FastAPI()
    app.websocket("/xiaoyu/chat-docs/chat/{local_doc_id}")(stream_chat)
    app.post("/xiaoyu/chat-docs/upload")(upload_file)
    app.get("/xiaoyu/chat-docs/list")(list_docs)
    app.delete("/xiaoyu/chat-docs/delete")(delete_docs)
    uvicorn.run(app, host="0.0.0.0", port=7861)


if __name__ == "__main__":
    main()
