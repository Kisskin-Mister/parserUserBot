from pyrogram import enums


def is_recruiter_private_message(message) -> bool:
    return (
        message.chat.type == enums.ChatType.PRIVATE
        and message.from_user is not None
        and not message.from_user.is_self
    )


def should_mark_chat_as_read(message) -> bool:
    return not is_recruiter_private_message(message)
