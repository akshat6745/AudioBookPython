import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.api.novels import extract_paragraphs_from_soup
from bs4 import BeautifulSoup


test_data1 = {
    "chapter_title_text": "Chapter 1: The Beginning",
    "soup": """
    <div class="chapter-content">
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first paragraph of the chapter.</p>
        <p>This is the second paragraph of the chapter.</p>
        <p>This is the third paragraph of the chapter.</p>
        <p>This is the fourth paragraph of the chapter.</p>
        <p>This is the fifth paragraph of the chapter.</p>
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter 1: The Beginning",
        "paragraphs": [
            "This is the first paragraph of the chapter.",
            "This is the second paragraph of the chapter.",
            "This is the third paragraph of the chapter.",
            "This is the fourth paragraph of the chapter.",
            "This is the fifth paragraph of the chapter."
        ]
    }
}

test_data2 = {
    "chapter_title_text": "Chapter 1: The Beginning",
    "soup": """
    <div class="chapter-content">
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first paragraph of the chapter.</p>
        <p>This is the second paragraph of the chapter.</p>
        <p>This is the third paragraph of the chapter.</p>
        <p>This is the fourth paragraph of the chapter.</p>
        This is the fifth paragraph of the chapter.
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter 1: The Beginning",
        "paragraphs": [
            "This is the first paragraph of the chapter.",
            "This is the second paragraph of the chapter.",
            "This is the third paragraph of the chapter.",
            "This is the fourth paragraph of the chapter.",
            "This is the fifth paragraph of the chapter."
        ]
    }
}

test_data3 = {
    "chapter_title_text": "Chapter 1: The Beginning",
    "soup": """
    <div class="chapter-content">
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first paragraph of the chapter.</p>
        <p>This is the second paragraph of<em> the </em>chapter.</p>
        <p>This is the third paragraph of<em> the</em> chapter.</p>
        <p>This is the fourth <em>paragraph</em> of the chapter.</p>
        This is the fifth paragraph of <em>the</em> chapter.
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter 1: The Beginning",
        "paragraphs": [
            "This is the first paragraph of the chapter.",
            "This is the second paragraph of the chapter.",
            "This is the third paragraph of the chapter.",
            "This is the fourth paragraph of the chapter.",
            "This is the fifth paragraph of the chapter."
        ]
    }
}

def run_test_case(test_name: str, test_data: dict):
    """Run a single test case and compare the output."""
    print(f"\n=== Running {test_name} ===")
    
    # Parse the HTML soup
    soup = BeautifulSoup(test_data["soup"], 'html.parser')
    
    # Run the function
    try:
        actual_output = extract_paragraphs_from_soup(soup, test_data["chapter_title_text"])
        
        # Convert to expected format for comparison
        normalized_actual = {
            "chapter_title": actual_output["chapterTitle"],
            "paragraphs": actual_output["content"]
        }
        
        expected_output = test_data["expected_output"]
        
        # Compare outputs
        print(f"Expected: {expected_output}")
        print(f"Actual:   {normalized_actual}")
        
        # Check if they match
        if normalized_actual == expected_output:
            print("✅ TEST PASSED")
            return True
        else:
            print("❌ TEST FAILED")
            print("Differences:")
            if normalized_actual["chapter_title"] != expected_output["chapter_title"]:
                print(f"  Chapter title: expected '{expected_output['chapter_title']}', got '{normalized_actual['chapter_title']}'")
            if normalized_actual["paragraphs"] != expected_output["paragraphs"]:
                print(f"  Paragraphs differ:")
                print(f"    Expected: {expected_output['paragraphs']}")
                print(f"    Actual:   {normalized_actual['paragraphs']}")
            return False
            
    except Exception as e:
        print(f"❌ TEST FAILED WITH EXCEPTION: {e}")
        return False

def run_all_tests():
    """Run all test cases."""
    print("Starting novel content extraction tests...")
    
    test_cases = [
        ("Test Case 1 - Basic paragraphs", test_data1),
        ("Test Case 2 - Mixed content with loose text", test_data2),
        ("Test Case 3 - Paragraphs with emphasized text", test_data3)
    ]
    
    passed = 0
    total = len(test_cases)
    
    for test_name, test_data in test_cases:
        if run_test_case(test_name, test_data):
            passed += 1
    
    print(f"\n=== TEST SUMMARY ===")
    print(f"Passed: {passed}/{total}")
    print(f"Success rate: {(passed/total)*100:.1f}%")
    
    return passed == total

if __name__ == "__main__":
    run_all_tests()