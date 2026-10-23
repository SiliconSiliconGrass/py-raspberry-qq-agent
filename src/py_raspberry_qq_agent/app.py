from __future__ import annotations

import logging
from dataclasses import dataclass

from .agent import AgentRunner, AgentWorker
from .config import AppConfig
from .contacts import ContactDirectory, ContactVault, RoleStore
from .digest import GroupDigestService
from .incoming import IncomingService
from .knowledge import KnowledgeBase
from .llm import OpenAICompatibleClient
from .logging_setup import configure_logging
from .mcp_runtime import McpManager
from .messages import MessageStore
from .napcat import IncomingMessage, NapCatClient, OutboundQueue
from .priority import ImportanceGate
from .scheduler import ScheduledTask, Scheduler, ScheduleStore
from .todos import TodoStore
from .toolset import HistoryService, build_tool_registry

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Application:
    config: AppConfig
    vault: ContactVault
    roles: RoleStore
    contacts: ContactDirectory
    todos: TodoStore
    schedules: ScheduleStore
    messages: MessageStore
    knowledge: KnowledgeBase
    main_llm: OpenAICompatibleClient
    classifier_llm: OpenAICompatibleClient
    mcp: McpManager
    napcat: NapCatClient
    outbound: OutboundQueue
    history: HistoryService
    incoming: IncomingService
    digest: GroupDigestService
    worker: AgentWorker | None = None
    scheduler: Scheduler | None = None
    _worker_ref: dict[str, AgentWorker | None] | None = None

    @classmethod
    def build(cls, config: AppConfig) -> Application:
        config.prepare_directories()
        configure_logging(config)

        vault = ContactVault(config.data_dir / "contacts.json", config.encryption_key)
        roles = RoleStore(config.data_dir / "roles.json")
        for raw_admin_id in config.admin_qq_ids:
            roles.set_admin(vault.encrypt("user", raw_admin_id), True)
        contacts = ContactDirectory(config.data_dir / "profiles.json")
        todos = TodoStore(config.data_dir / "todos.json")
        schedules = ScheduleStore(config.data_dir / "schedules.json", config.timezone)
        messages = MessageStore(
            config.data_dir / "messages.jsonl",
            max_fetch=config.history_max_fetch,
            max_content_chars=config.history_content_chars,
        )
        knowledge = KnowledgeBase(
            config.knowledge_dir,
            config.notebook_dir,
            max_file_bytes=config.knowledge_max_file_bytes,
            max_results=config.knowledge_max_search_results,
            max_read_lines=config.knowledge_max_read_lines,
        )
        main_llm = OpenAICompatibleClient(
            config.main_llm, retry_count=config.main_llm.retry_count
        )
        classifier_llm = OpenAICompatibleClient(
            config.classifier_llm, retry_count=config.classifier_llm.retry_count
        )
        mcp = McpManager(config)

        worker_ref: dict[str, AgentWorker | None] = {"worker": None}

        def notify_worker() -> None:
            worker = worker_ref["worker"]
            if worker is not None:
                worker.notify()

        incoming: IncomingService

        async def on_napcat_message(message: IncomingMessage) -> None:
            await incoming.handle(message)

        napcat = NapCatClient(config, on_napcat_message)
        outbound = OutboundQueue(config, vault, napcat, messages)
        gate = ImportanceGate(config, classifier_llm, roles, contacts, messages)
        history = HistoryService(config, messages, napcat, vault)
        incoming = IncomingService(
            vault=vault,
            contacts=contacts,
            messages=messages,
            gate=gate,
            todos=todos,
            notify_worker=notify_worker,
            dedupe_messages=config.history_dedupe_messages,
        )
        digest = GroupDigestService(
            config=config,
            vault=vault,
            history=history,
            todos=todos,
            notify_worker=notify_worker,
            state_path=config.data_dir / "digest_state.json",
        )
        application = cls(
            config=config,
            vault=vault,
            roles=roles,
            contacts=contacts,
            todos=todos,
            schedules=schedules,
            messages=messages,
            knowledge=knowledge,
            main_llm=main_llm,
            classifier_llm=classifier_llm,
            mcp=mcp,
            napcat=napcat,
            outbound=outbound,
            history=history,
            incoming=incoming,
            digest=digest,
            _worker_ref=worker_ref,
        )
        return application

    async def start(self) -> None:
        await self.mcp.start()
        registry = build_tool_registry(
            config=self.config,
            todos=self.todos,
            schedules=self.schedules,
            knowledge=self.knowledge,
            contacts=self.contacts,
            outbound=self.outbound,
            history=self.history,
            mcp=self.mcp,
        )
        self.worker = AgentWorker(
            self.config,
            AgentRunner(self.config, self.main_llm, registry),
            self.todos,
        )
        if self._worker_ref is not None:
            self._worker_ref["worker"] = self.worker
        self.scheduler = Scheduler(
            self.schedules,
            self._enqueue_scheduled_task,
            self.config.scheduler_tick_seconds,
        )
        self.outbound.start()
        self.worker.start()
        if self.config.scheduler_enabled:
            self.scheduler.start()
        self.digest.start()
        self.napcat.start()
        LOGGER.info(
            "application started timezone=%s tools=%d",
            self.config.timezone,
            len(registry.definitions()),
        )

    async def _enqueue_scheduled_task(self, task: ScheduledTask) -> None:
        if self.worker is None:
            raise RuntimeError("agent worker is not started")
        item = await self.todos.add(
            content=f"处理定时任务：{task.content}",
            source="scheduler",
            context=(
                f"标题：{task.title}\n"
                f"任务：{task.content}\n"
                f"目标：{task.chat_type or '-'} "
                f"{task.encrypted_target_id or '-'}\n"
            ),
            chat_type=task.chat_type,
            encrypted_target_id=task.encrypted_target_id,
            metadata={"kind": "scheduled_task", "task_id": task.id},
        )
        LOGGER.info(
            "scheduled task added to TODO todo=%s task=%s", item.id, task.id
        )
        self.worker.notify()

    async def stop(self) -> None:
        LOGGER.info("application stopping")
        await self.napcat.stop()
        await self.digest.stop()
        if self.scheduler is not None and self.config.scheduler_enabled:
            await self.scheduler.stop()
        await self.outbound.stop()
        if self.worker is not None:
            await self.worker.stop()
        await self.mcp.stop()
        await self.main_llm.close()
        await self.classifier_llm.close()
        LOGGER.info("application stopped")
