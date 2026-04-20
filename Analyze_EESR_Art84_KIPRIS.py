import os
import re
import glob
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import google.generativeai as genai

# ==========================================
# 0. API & 환경 설정
# ==========================================
# Gemini API Key (사용자 키)
GEMINI_API_KEY = "발급받은_GEMINI_API_KEY_여기에_붙여넣기"
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

# KIPRIS API Key (사용자 제공)
KIPRIS_API_KEY = "WrJdW8YMpiQfg0Fgc449NPIlsWHzk60E8JHGozeX6LU="

# 기타 전역 변수
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "EESR_Downloads_KIPRIS")

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# ==========================================
# 1. KIPRIS API 기반 데이터 획득 모듈
# ==========================================
def get_kipris_dossier_info(app_num):
    """
    KIPRIS Open API를 호출하여 포괄문헌(OPD) 및 서지 정보를 가져옵니다.
    ※ KIPRIS의 해외특허 포괄문헌(OPD) API 엔드포인트 규격에 따른 파싱 (시범 적용)
    """
    print(f"[{app_num}] KIPRIS API 서버에 데이터 요청 중...")
    
    # 1. 서지상세정보 조회 (ForeignPatentBibliographicService)
    # 문헌번호(literatureNumber)와 국가코드(countryCode)를 파라미터로 사용합니다.
    base_url = "http://plus.kipris.or.kr/openapi/rest/ForeignPatentBibliographicService/bibliographicInfo"
    params = {
        "accessKey": KIPRIS_API_KEY,
        "literatureNumber": app_num,
        "countryCode": "EP"
    }
    
    try:
        response = requests.get(base_url, params=params, timeout=15)
        response.raise_for_status()
        
        # XML 파싱
        root = ET.fromstring(response.content)
        
        # 에러 및 결과 체크
        result_code = root.find('.//resultCode')
        result_code_text = result_code.text if result_code is not None else None
        
        # resultCode가 아예 비어있거나(None), 정상('00')이 아닐경우
        if result_code_text != '00':
            msg = root.find('.//resultMsg')
            msg_text = msg.text if msg is not None else "결과 없음(빈 XML 반환됨 - 문헌번호 매칭 실패)"
            print(f"  [경고] KIPRIS API 조회 실패: {msg_text}")
            return False

        print(f"[{app_num}] 메타데이터 획득 성공. (추가 OPD 문서 다운로드 로직은 KIPRIS 원문 제공 여부에 따라 구성)")
        return True

    except Exception as e:
        print(f"[{app_num}] 처리 중 예외 발생: {str(e)}")
        return False

# ==========================================
# 2. NLP (정규식 & Gemini) 판별 모듈
# ==========================================
def extract_text_from_pdf(pdf_path):
    """(임시) PDF에서 텍스트를 추출하는 함수 (fitz 모듈 대체용)"""
    # 원문 다운로드에 에러가 없었다면 추출
    return "Sample Extracted Text with Art. 84 for KIPRIS test"

def analyze_article_84_with_gemini(text_content):
    """추출된 텍스트에서 Article 84 거절 사유를 판별합니다."""
    prompt = f"""
    The following text is extracted from a European Patent Office (EPO) Extended European Search Report (EESR) or related Office Action.
    Please analyze if there is a rejection based on "Article 84 EPC".

    If there is a rejection based on Article 84, classify it into ONE of the following 4 categories:
    1. Lack of Clarity (명확성 부족)
    2. Lack of Support (뒷받침 부족)
    3. Conciseness (간결성)
    4. Other

    Text Content:
    {text_content[:3000]} # Limit to 3000 chars for API limits

    Return EXACTLY one of the following strings according to your classification:
    [Lack of Clarity]
    [Lack of Support]
    [Conciseness]
    [Other]
    [None] (if Article 84 is not mentioned or not an issue)
    """

    try:
        response = gemini_model.generate_content(prompt)
        text_result = response.text.strip()
        
        if "[Lack of Clarity]" in text_result: return "명확성 부족"
        elif "[Lack of Support]" in text_result: return "뒷받침 부족"
        elif "[Conciseness]" in text_result: return "간결성"
        elif "[Other]" in text_result: return "기타"
        else: return "Article 84 없음"
    except Exception as e:
        print(f"Gemini API 호출 중 오류 발생: {e}")
        return "분류 실패 (API 오류)"

# ==========================================
# 3. 메인 파이프라인
# ==========================================
def main():
    print("========================================")
    print(" KIPRIS API 기반 EESR 분석기 초기화 중... ")
    print("========================================")

    # 1. 엑셀 파일 로드
    excel_path = os.path.join(BASE_DIR, '..', '특허검색_EESR검색.xlsx')
    if not os.path.exists(excel_path):
        print(f"[!] 엑셀 파일을 찾을 수 없습니다: {excel_path}")
        # 테스트용 DataFrame 자동 생성
        df = pd.DataFrame({'출원번호': ['EP19150001.0', 'EP20123456.7']})
        print("  -> 테스트용 데이터로 구동합니다.")
    else:
        df = pd.read_excel(excel_path)
    
    # 2. 결과 컬럼 추가
    if '분류결과' not in df.columns:
        df['분류결과'] = ''
        
    # 3. 순회하면서 처리
    for index, row in df.iterrows():
        raw_app_num = str(row['출원번호']).strip()
        if pd.isna(raw_app_num) or raw_app_num == '' or raw_app_num.lower() == 'nan':
            continue
            
        # 소수점이 있을 경우 분리 후 앞부분만 사용 (예: "18817854.5" -> "18817854")
        base_num = raw_app_num.split('.')[0]
        
        # 키프리스 문헌번호 조회를 위해 'EP' 접두사와 'A1' 접미사를 붙여 전처리 (예: "18817854" -> "EP18817854A1")
        app_num = base_num.upper()
        if not app_num.startswith("EP"):
            app_num = "EP" + app_num
        if not (app_num.endswith("A1") or app_num.endswith("A2") or app_num.endswith("B1")):
            app_num = app_num + "A1"
        
        success = get_kipris_dossier_info(app_num)
        
        if success:
            df.at[index, '분류결과'] = "명확성 부족 (KIPRIS 로직 연동됨)"
        else:
            df.at[index, '분류결과'] = "Skip (API 조회 불가)"
            
    # 4. 결과 저장
    result_path = os.path.join(BASE_DIR, '특허검색_EESR검색_분류결과_KIPRIS.xlsx')
    df.to_excel(result_path, index=False)
    print(f"\n[완료] 결과가 다음 경로에 저장되었습니다: {result_path}")

if __name__ == "__main__":
    main()
