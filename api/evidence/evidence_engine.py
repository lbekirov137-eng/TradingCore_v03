from api.evidence.evidence_result import EvidenceResult


class EvidenceEngine:

    NAME = "Evidence Engine"
    VERSION = "1.0.0"

    @staticmethod
    def validate(candidate) -> EvidenceResult:
        """
        Главная точка проверки новой идеи.

        Пока возвращает пустой результат.
        """
        return EvidenceResult(
            approved=False,
            stage="INITIAL",
            message="Evidence Engine пока не реализован."
        )