from api.analyzer import MarketAnalyzer
import json


def main():
    result = MarketAnalyzer.analyze()
    print(json.dumps(result, indent=4))


if __name__ == "__main__":
    main()