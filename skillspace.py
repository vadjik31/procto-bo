import httpx


class SkillspaceError(RuntimeError):
    pass


async def invite_student(
    *,
    api_key: str,
    email: str,
    name: str = "",
    course_id: str,
    group_id: str = "",
    base_url: str = "https://skillspace.ru",
) -> None:
    """
    Skillspace API:
    POST/GET {base_url}/api/open/v1/course/student-invite

    courses передаётся как php-array:
      courses[COURSE_ID]=GROUP_ID
    group_id может быть пустым — Skillspace выберет автоматически.
    """
    url = f"{base_url.rstrip('/')}/api/open/v1/course/student-invite"

    params = {
        "token": api_key,
        "email": email,
        "name": name or "",
        f"courses[{course_id}]": group_id or "",
    }

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(url, params=params)

    if r.status_code == 200:
        return

    raise SkillspaceError(f"Invite failed: {r.status_code} | {r.text}")
