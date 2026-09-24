# CLAUDE.md

이 파일은 Claude Code가 이 저장소에서 작업할 때 참고하는 안내서입니다.

## 프로젝트 개요
- 이 프로젝트는 **기획** 프로젝트입니다.
- 현재 기획 단계로, 주요 산출물은 `/docs`의 기획 문서입니다. 아직 소스 코드는 없습니다.
- 구현 단계에서는 Python 데스크톱 GUI(Tkinter / CustomTkinter)로 개발할 예정입니다. 구조가 생기면 이 문서를 함께 갱신하세요.

## 폴더 구조
- `/src` : 소스 코드
- `/docs` : 문서
- `/tests` : 테스트 코드
- `main.py` : 실행 진입점

## 사용 기술
- Python 3.11
- Tkinter, CustomTkinter

## 빌드/실행
```bash
pip install -r requirements.txt   # 필요한 라이브러리 설치
python main.py                    # 앱 실행
```

## 테스트
```bash
python -m pytest tests
```

## 코드 스타일
- PEP8 준수
- 함수·변수: `snake_case`
- 클래스: `PascalCase`
- 상수: `UPPER_SNAKE_CASE`

## 작업 규칙
- 변경 후 테스트가 통과하는지 확인한 뒤 커밋합니다.
- 기능 변경 시 `/docs` 및 이 문서 등 관련 문서를 함께 업데이트합니다.
- PR은 테스트 통과 후 리뷰를 요청합니다.
- 규칙은 `.github/copilot-instructions.md`와 일치하도록 유지합니다.
